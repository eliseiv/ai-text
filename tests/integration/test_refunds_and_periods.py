"""Subscription periods per product (CloudPayments) and refunds across the three channels.

Money rules pinned here:
* one Apple purchase that reaches us through StoreKit sync AND the Adapty webhook is credited ONCE;
* a refund takes back what the purchase granted — once, whichever channels report it — never
  below zero; a refunded subscription loses access;
* the RU refund callback is only a trigger: the refund must be confirmed by our verify call, and
  only payments WE credited are reversed.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.products import add_period, get_products, parse_products
from tests.conftest import (
    ADAPTY_SECRET,
    CLOUDPAYMENTS_API_BASE,
    FakeStoreKitVerifier,
    auth_headers,
    balance_of,
    seed_user,
)

WEEKLY = "week_6.99_nottrial"
YEARLY = "yearly_49.99_nottrial"
PACK = "100_Tokens_9.99"
ALL = ["apple_storekit", "adapty", "cloudpayments"]
CATALOGUE = {
    WEEKLY: {"kind": "subscription", "credits": 100, "channels": ALL, "period": "P1W"},
    YEARLY: {"kind": "subscription", "credits": 800, "channels": ALL, "period": "P1Y"},
    PACK: {"kind": "tokens", "credits": 100, "channels": ALL},
}
DEVICE = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
ADAPTY = "/v1/billing/adapty/webhook"
ADAPTY_AUTH = {"Authorization": f"Bearer {ADAPTY_SECRET}"}
CP = "/v1/billing/cloudpayments/webhook"


@pytest.fixture(autouse=True)
def catalogue(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PRODUCTS", json.dumps(CATALOGUE))
    get_settings.cache_clear()
    get_products.cache_clear()
    yield
    get_settings.cache_clear()
    get_products.cache_clear()


async def _subscription(session: AsyncSession, user_id: Any) -> tuple[str, datetime.datetime]:
    row = (
        await session.execute(
            text("SELECT status, expires_at FROM subscriptions WHERE user_id = :u"),
            {"u": str(user_id)},
        )
    ).first()
    assert row is not None
    return row[0], row[1]


async def _payment_rows(session: AsyncSession, user_id: Any) -> list[tuple[str, str]]:
    rows = (
        await session.execute(
            text("SELECT kind, status FROM payments WHERE user_id = :u ORDER BY received_at"),
            {"u": str(user_id)},
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


def _adapty(event_id: str, event_type: str, txn: str, product: str) -> dict[str, Any]:
    return {
        "profile_event_id": event_id,
        "event_type": event_type,
        "customer_user_id": DEVICE.lower(),
        "event_properties": {"vendor_product_id": product, "transaction_id": txn},
    }


# --- catalogue -------------------------------------------------------------------------------
def test_period_is_validated_and_required_for_ru_subscriptions() -> None:
    parsed = parse_products(
        json.dumps(
            {
                "ok": {
                    "kind": "subscription",
                    "credits": 1,
                    "channels": ["cloudpayments"],
                    "period": "p1w",
                },
                "bad": {
                    "kind": "subscription",
                    "credits": 1,
                    "channels": ["adapty"],
                    "period": "weekly",
                },
                "no_period_ru": {
                    "kind": "subscription",
                    "credits": 1,
                    "channels": ["cloudpayments"],
                },
                "no_period_apple": {"kind": "subscription", "credits": 1, "channels": ["adapty"]},
            }
        )
    )
    assert parsed["ok"].period == "P1W"
    assert "bad" not in parsed and "no_period_ru" not in parsed
    assert parsed["no_period_apple"].period is None  # Apple/Adapty send the expiry themselves


def test_calendar_periods() -> None:
    start = datetime.datetime(2026, 1, 31, 12, tzinfo=datetime.UTC)
    assert add_period(start, "P1W") == start + datetime.timedelta(days=7)
    assert add_period(start, "P1M") == datetime.datetime(2026, 2, 28, 12, tzinfo=datetime.UTC)
    assert add_period(start, "P1Y") == datetime.datetime(2027, 1, 31, 12, tzinfo=datetime.UTC)


# --- CloudPayments: the term follows the product ----------------------------------------------
def _cp_payment(
    pid: str, product: str, status: str = "succeeded", ptype: str = "subscription"
) -> dict[str, Any]:
    return {
        "payment_id": pid,
        "status": status,
        "paid_at": (
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=5)
        ).isoformat(),
        "product": {"code": product, "payment_type": ptype},
    }


def _cp_callback(operation: str = "Payment") -> dict[str, Any]:
    return {
        "Status": "Completed",
        "OperationType": operation,
        "AccountId": DEVICE,
        "Data": json.dumps({"user_id": DEVICE}),
    }


def _verify(payments: list[dict[str, Any]]) -> None:
    respx.get(f"{CLOUDPAYMENTS_API_BASE}/users/{DEVICE}/payments").mock(
        return_value=httpx.Response(200, json={"data": payments})
    )


@respx.mock
@pytest.mark.parametrize(("product", "days"), [(WEEKLY, 7), (YEARLY, 365)])
async def test_ru_subscription_lasts_exactly_its_period(
    client: AsyncClient, session: AsyncSession, product: str, days: int
) -> None:
    user_id = await seed_user(session, device_id=DEVICE)
    _verify([_cp_payment("p1", product)])
    assert (await client.post(CP, json=_cp_callback())).status_code == 200
    status, expires = await _subscription(session, user_id)
    remaining = expires - datetime.datetime.now(tz=datetime.UTC)
    assert status == "active"
    assert datetime.timedelta(days=days - 1) < remaining <= datetime.timedelta(days=days)


@respx.mock
async def test_ru_early_renewal_extends_from_the_current_expiry(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE)
    _verify([_cp_payment("p1", WEEKLY)])
    await client.post(CP, json=_cp_callback())
    _, first = await _subscription(session, user_id)
    _verify([_cp_payment("p1", WEEKLY), _cp_payment("p2", WEEKLY)])
    await client.post(CP, json=_cp_callback())
    _, second = await _subscription(session, user_id)
    assert second - first == datetime.timedelta(days=7)


@respx.mock
async def test_ru_refund_is_confirmed_by_verify_and_reverses_once(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE)
    _verify([_cp_payment("p1", WEEKLY)])
    await client.post(CP, json=_cp_callback())
    assert await balance_of(session, user_id) == 100

    # A refund callback the aggregator does NOT confirm changes nothing.
    await client.post(CP, json=_cp_callback("Refund"))
    assert await balance_of(session, user_id) == 100

    _verify([_cp_payment("p1", WEEKLY, status="refunded")])
    for _ in range(2):  # the re-delivered callback must not take the credits twice
        response = await client.post(CP, json=_cp_callback("Refund"))
        assert response.status_code == 200
    assert await balance_of(session, user_id) == 0
    status, _ = await _subscription(session, user_id)
    assert status == "expired"
    assert await _payment_rows(session, user_id) == [
        ("subscription", "granted"),
        ("refund", "refunded"),
    ]


@respx.mock
async def test_ru_refund_of_a_payment_we_never_credited_is_ignored(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE, balance=50)
    _verify([_cp_payment("old", WEEKLY, status="refunded")])
    await client.post(CP, json=_cp_callback("Refund"))
    assert await balance_of(session, user_id) == 50
    assert await _payment_rows(session, user_id) == []


# --- Apple: StoreKit + Adapty ---------------------------------------------------------------
async def test_one_apple_purchase_via_storekit_and_adapty_is_credited_once(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session, device_id=DEVICE.lower())
    fake_storekit.script(transaction_id="A1", product_id=WEEKLY, expires_in_days=7)
    r = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    assert r.json()["creditsGranted"] == 100
    await client.post(
        ADAPTY, json=_adapty("E1", "subscription_started", "A1", WEEKLY), headers=ADAPTY_AUTH
    )
    assert await balance_of(session, user_id) == 100


async def test_adapty_refund_takes_credits_back_and_ends_access_once(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session, device_id=DEVICE.lower())
    await client.post(
        ADAPTY, json=_adapty("E1", "subscription_started", "A1", WEEKLY), headers=ADAPTY_AUTH
    )
    assert await balance_of(session, user_id) == 100

    refund = _adapty("E2", "subscription_refunded", "A1", WEEKLY)
    assert (await client.post(ADAPTY, json=refund, headers=ADAPTY_AUTH)).status_code == 200
    await client.post(ADAPTY, json=refund, headers=ADAPTY_AUTH)  # re-delivery
    assert await balance_of(session, user_id) == 0
    assert (await _subscription(session, user_id))[0] == "expired"

    # The same refund seen through StoreKit (a revoked transaction) does not take it twice.
    fake_storekit.script(transaction_id="A1", product_id=WEEKLY, expires_in_days=7, revoked=True)
    r = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    assert r.json()["status"] == "expired" and r.json()["creditsGranted"] == 0
    debits = await session.scalar(
        text("SELECT count(*) FROM ledger_transactions WHERE user_id = :u AND type = 'debit'"),
        {"u": str(user_id)},
    )
    assert debits == 1


async def test_refund_after_credits_were_spent_stops_at_zero(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE.lower())
    await client.post(
        ADAPTY, json=_adapty("E1", "subscription_started", "A1", WEEKLY), headers=ADAPTY_AUTH
    )
    await session.execute(
        text("UPDATE wallets SET balance = 30 WHERE user_id = :u"), {"u": str(user_id)}
    )
    await session.commit()
    await client.post(
        ADAPTY, json=_adapty("E2", "subscription_refunded", "A1", WEEKLY), headers=ADAPTY_AUTH
    )
    assert await balance_of(session, user_id) == 0
    payload = await session.scalar(
        text("SELECT payload FROM payments WHERE user_id = :u AND kind = 'refund'"),
        {"u": str(user_id)},
    )
    assert payload["creditsRevoked"] == 30 and payload["creditsNotRecovered"] == 70


async def test_adapty_token_pack_is_granted_and_refunded_without_touching_the_subscription(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(
        session, device_id=DEVICE.lower(), subscription="active", plan=WEEKLY, balance=0
    )
    await client.post(
        ADAPTY, json=_adapty("E1", "non_subscription_purchase", "P1", PACK), headers=ADAPTY_AUTH
    )
    assert await balance_of(session, user_id) == 100
    # The same consumable synced by the client through StoreKit: replay, not a second grant.
    fake_storekit.script(transaction_id="P1", product_id=PACK, expires_in_days=None)
    r = await client.post(
        "/v1/tokens/purchase", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    assert r.json()["idempotentReplay"] is True and await balance_of(session, user_id) == 100

    await client.post(
        ADAPTY,
        json=_adapty("E2", "non_subscription_purchase_refunded", "P1", PACK),
        headers=ADAPTY_AUTH,
    )
    assert await balance_of(session, user_id) == 0
    assert (await _subscription(session, user_id))[0] == "active"


async def test_revoked_storekit_transaction_never_grants(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session)
    fake_storekit.script(transaction_id="R1", product_id=WEEKLY, expires_in_days=7, revoked=True)
    r = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    assert r.status_code == 200 and r.json()["creditsGranted"] == 0
    assert await balance_of(session, user_id) == 0


async def test_revoked_storekit_token_pack_takes_its_credits_back(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session, subscription="active", plan=WEEKLY, balance=0)
    fake_storekit.script(transaction_id="T9", product_id=PACK, expires_in_days=None)
    r = await client.post(
        "/v1/tokens/purchase", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    assert r.json()["creditsAdded"] == 100

    fake_storekit.script(transaction_id="T9", product_id=PACK, expires_in_days=None, revoked=True)
    for _ in range(2):  # a repeated sync of the revoked transaction takes nothing twice
        r = await client.post(
            "/v1/tokens/purchase", json={"transaction": "jws"}, headers=auth_headers(user_id)
        )
        assert r.status_code == 200 and r.json()["creditsAdded"] == 0
    assert await balance_of(session, user_id) == 0
    assert (await _subscription(session, user_id))[0] == "active"  # a pack refund keeps access
