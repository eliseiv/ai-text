"""The public RU webhook: callback = TRIGGER, our own verify = TRUTH.

The aggregator sends NO authorization at all, so a forged callback must be harmless: it may at
most trigger a useless verify GET, which confirms nothing → zero credits.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from tests.conftest import (
    CLOUDPAYMENTS_API_BASE,
    PRODUCT_SUB,
    PRODUCT_SUB_APPLE_ONLY,
    PRODUCT_SUB_CREDITS,
    PRODUCT_TOKENS,
    PRODUCT_TOKENS_CREDITS,
    balance_of,
    deny_limiter,
    seed_user,
)

# The device id as iOS actually stores it: identifierForVendor.uuidString is UPPERCASE.
DEVICE_UPPER = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
DEVICE_LOWER = DEVICE_UPPER.lower()

WEBHOOK = "/v1/billing/cloudpayments/webhook"


def callback(account_id: str = DEVICE_UPPER, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "Status": "Completed",
        "OperationType": "Payment",
        "AccountId": account_id,
        "TransactionId": 777,
        "CardLastFour": "1111",
        "Data": json.dumps({"user_id": account_id}),
    }
    body.update(overrides)
    return body


def payment(
    payment_id: str = "pay-1",
    product_code: str = PRODUCT_SUB,
    payment_type: str = "subscription",
    status: str = "succeeded",
    hours_ago: float = 1,
) -> dict[str, Any]:
    paid_at = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=hours_ago)
    return {
        "payment_id": payment_id,
        "status": status,
        "paid_at": paid_at.isoformat(),
        "product": {"code": product_code, "payment_type": payment_type},
    }


def verify_route(device_id: str, payments: list[dict[str, Any]]) -> respx.Route:
    return respx.get(f"{CLOUDPAYMENTS_API_BASE}/users/{device_id}/payments").mock(
        return_value=httpx.Response(200, json={"data": payments})
    )


@respx.mock
async def test_callback_without_authorization_is_accepted_and_credits_after_verify(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    route = verify_route(DEVICE_UPPER, [payment()])

    response = await client.post(WEBHOOK, json=callback())

    assert response.status_code == 200  # no 401: requiring a token would lose every RU payment
    assert response.json() == {"code": 0}
    assert route.called
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS

    row = (
        await session.execute(
            text(
                "SELECT external_id, status, credits_granted, kind FROM payments "
                "WHERE user_id = :u"
            ),
            {"u": str(user_id)},
        )
    ).first()
    assert row is not None
    assert row[0] == "pay-1"  # the payment_id FROM VERIFY, never the callback's TransactionId
    assert row[1] == "granted" and int(row[2]) == PRODUCT_SUB_CREDITS and row[3] == "subscription"

    subscription = (
        await session.execute(
            text("SELECT status, plan FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
        )
    ).first()
    assert subscription is not None and subscription[0] == "active"


@respx.mock
async def test_redelivery_is_idempotent_and_does_not_extend_the_subscription(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A replayed payment must NOT re-extend access: the aggregator re-delivers callbacks
    routinely, and an extension per delivery would hand out unpaid months."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    verify_route(DEVICE_UPPER, [payment()])

    await client.post(WEBHOOK, json=callback())
    first_expiry = await session.scalar(
        text("SELECT expires_at FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
    )

    await client.post(WEBHOOK, json=callback())  # the very same payment, again
    second_expiry = await session.scalar(
        text("SELECT expires_at FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
    )

    assert second_expiry == first_expiry, "a replayed delivery must not move expires_at"
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS  # credited once
    count = await session.scalar(
        text("SELECT count(*) FROM payments WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert int(count or 0) == 1


@respx.mock
async def test_verify_confirming_nothing_credits_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A FORGED callback ends here: we never trust it, we ask the aggregator ourselves."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    verify_route(DEVICE_UPPER, [])

    response = await client.post(WEBHOOK, json=callback())

    assert response.status_code == 200
    assert await balance_of(session, user_id) == 0
    assert int(await session.scalar(text("SELECT count(*) FROM payments")) or 0) == 0


@respx.mock
async def test_only_fresh_paid_payments_are_credited(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    verify_route(
        DEVICE_UPPER,
        [
            payment("old", hours_ago=1000),  # outside the freshness window
            payment("pending-1", status="pending"),
            payment("fresh", product_code=PRODUCT_TOKENS, payment_type="one_time"),
        ],
    )

    await client.post(WEBHOOK, json=callback())

    assert await balance_of(session, user_id) == PRODUCT_TOKENS_CREDITS
    rows = (
        await session.execute(
            text("SELECT external_id FROM payments WHERE user_id = :u"), {"u": str(user_id)}
        )
    ).all()
    assert [r[0] for r in rows] == ["fresh"]


@respx.mock
async def test_verify_outage_is_a_retriable_500(client: AsyncClient, session: AsyncSession) -> None:
    """200 would mean "accepted" to the aggregator → it never re-delivers → the payment is lost
    forever. 500 makes it come back."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    respx.get(f"{CLOUDPAYMENTS_API_BASE}/users/{DEVICE_UPPER}/payments").mock(
        side_effect=httpx.TimeoutException("timeout")
    )

    response = await client.post(WEBHOOK, json=callback())

    assert response.status_code == 500
    assert await balance_of(session, user_id) == 0


@respx.mock
async def test_user_not_found_does_not_call_verify(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Anti-amplification: the endpoint is public — a garbage callback must not make us call the
    aggregator."""
    route = verify_route(DEVICE_UPPER, [payment()])
    response = await client.post(WEBHOOK, json=callback())

    assert response.status_code == 200
    assert route.call_count == 0
    assert int(await session.scalar(text("SELECT count(*) FROM users")) or 0) == 0
    assert int(await session.scalar(text("SELECT count(*) FROM payments")) or 0) == 0


@respx.mock
async def test_foreign_channel_product_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A CloudPayments payment naming an APPLE-only productId gets NOTHING (the source's hole)."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    verify_route(DEVICE_UPPER, [payment("pay-apple", product_code=PRODUCT_SUB_APPLE_ONLY)])

    await client.post(WEBHOOK, json=callback())

    assert await balance_of(session, user_id) == 0
    status = await session.scalar(
        text("SELECT status FROM payments WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert status == "rejected"


@respx.mock
async def test_unknown_product_is_rejected_with_zero_credits(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    verify_route(DEVICE_UPPER, [payment("pay-x", product_code="never.configured")])

    await client.post(WEBHOOK, json=callback())

    assert await balance_of(session, user_id) == 0
    status = await session.scalar(
        text("SELECT status FROM payments WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert status == "rejected"  # fail-closed: NO fallback grant


@respx.mock
async def test_non_completed_callback_is_ignored_without_touching_the_aggregator(
    client: AsyncClient, session: AsyncSession
) -> None:
    await seed_user(session, device_id=DEVICE_UPPER)
    route = verify_route(DEVICE_UPPER, [payment()])

    response = await client.post(WEBHOOK, json=callback(Status="Declined"))

    assert response.status_code == 200
    assert route.call_count == 0


@respx.mock
async def test_invalid_account_id_never_reaches_the_verify_path(
    client: AsyncClient,
) -> None:
    """Anti-SSRF: only a canonical UUID may ever be interpolated into the outgoing URL."""
    route = respx.get(url__regex=rf"{CLOUDPAYMENTS_API_BASE}/users/.*/payments").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    response = await client.post(WEBHOOK, json=callback(account_id="../../admin"))

    assert response.status_code == 200
    assert route.call_count == 0


@respx.mock
async def test_webhook_is_rate_limited_per_ip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    deny_limiter(monkeypatch, "enforce_cloudpayments_webhook_limits")
    response = await client.post(WEBHOOK, json=callback())
    assert response.status_code == 429


async def test_unconfigured_channel_is_a_retriable_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No API token ⇒ we cannot verify ⇒ we must not credit. 500 so the aggregator keeps retrying
    until the operator configures the channel."""
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "")
    get_settings.cache_clear()
    try:
        response = await client.post(WEBHOOK, json=callback())
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "cloudpayments_webhook_misconfigured"
    finally:
        get_settings.cache_clear()


@respx.mock
async def test_card_pii_never_reaches_the_stored_payload(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    verify_route(DEVICE_UPPER, [payment()])

    await client.post(WEBHOOK, json=callback(CardFirstSix="411111", CardLastFour="1111"))

    payload = await session.scalar(
        text("SELECT payload FROM payments WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert set(payload) <= {"paymentId", "productCode", "paymentType", "status", "paidAt"}
    assert "CardLastFour" not in json.dumps(payload)


@respx.mock
async def test_garbage_bodies_are_ignored_not_retried_forever(client: AsyncClient) -> None:
    for body in (b"", b"not json", b"[1,2,3]"):
        response = await client.post(WEBHOOK, content=body)
        assert response.status_code == 200


async def test_unknown_device_id_is_never_provisioned(
    client: AsyncClient, session: AsyncSession
) -> None:
    response = await client.post(WEBHOOK, json=callback(account_id=str(uuid.uuid4())))
    assert response.status_code == 200
    assert int(await session.scalar(text("SELECT count(*) FROM users")) or 0) == 0
