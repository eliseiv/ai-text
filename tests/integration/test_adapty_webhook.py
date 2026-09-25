"""Adapty webhook semantics: auth, the three event classes, access follows the
journal."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import (
    ADAPTY_SECRET,
    PRODUCT_SUB,
    PRODUCT_SUB_APPLE_ONLY,
    PRODUCT_SUB_CREDITS,
    balance_of,
    seed_user,
)

WEBHOOK = "/v1/billing/adapty/webhook"
DEVICE = "ADAPTY-DEVICE-1"
AUTH = {"Authorization": f"Bearer {ADAPTY_SECRET}"}


def event(
    event_id: str,
    event_type: str,
    *,
    transaction_id: str | None = "T1",
    product_id: str | None = PRODUCT_SUB,
    customer: str = DEVICE.lower(),
    **extra: Any,
) -> dict[str, Any]:
    properties: dict[str, Any] = {"vendor_product_id": product_id, **extra}
    if transaction_id is not None:
        properties["transaction_id"] = transaction_id
    return {
        "profile_event_id": event_id,
        "event_type": event_type,
        "customer_user_id": customer,
        "event_properties": properties,
    }


# --- authorization ---
async def test_webhook_requires_the_bearer_secret(client: AsyncClient) -> None:
    assert (await client.post(WEBHOOK, json=event("E", "subscription_started"))).status_code == 401
    wrong = await client.post(
        WEBHOOK,
        json=event("E", "subscription_started"),
        headers={"Authorization": "Bearer nope"},
    )
    assert wrong.status_code == 401


async def test_missing_secret_configuration_is_a_retriable_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("ADAPTY_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    try:
        response = await client.post(WEBHOOK, json={}, headers=AUTH)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "adapty_webhook_misconfigured"
    finally:
        get_settings.cache_clear()


# --- the three event classes ---
async def test_granting_event_activates_and_credits(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE)
    response = await client.post(
        WEBHOOK,
        json=event("E1", "subscription_started", subscription_expires_at="2035-01-01T00:00:00Z"),
        headers=AUTH,
    )
    assert response.json() == {"result": "applied", "reason": "granted"}
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS

    row = (
        await session.execute(
            text("SELECT status, plan FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
        )
    ).first()
    assert row is not None and row[0] == "active" and row[1] == PRODUCT_SUB


async def test_expiring_event_expires_the_subscription_without_credits(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE, subscription="active")
    response = await client.post(
        WEBHOOK, json=event("E2", "subscription_expired", transaction_id=None), headers=AUTH
    )

    assert response.json()["result"] == "applied"
    status = await session.scalar(
        text("SELECT status FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert status == "expired"
    assert await balance_of(session, user_id) == 0


async def test_renewal_cancelled_keeps_access(client: AsyncClient, session: AsyncSession) -> None:
    """Turning auto-renew off is NOT a refund — revoking here would rob a paying customer of the
    period he has already paid for."""
    user_id = await seed_user(session, device_id=DEVICE, subscription="active")
    response = await client.post(
        WEBHOOK,
        json=event("E3", "subscription_renewal_cancelled", transaction_id=None),
        headers=AUTH,
    )

    assert response.json() == {"result": "noop", "reason": "renewal_cancelled"}
    status = await session.scalar(
        text("SELECT status FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert status == "active"  # access KEPT


async def test_access_level_updated_is_conditional(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE)
    await client.post(
        WEBHOOK,
        json=event(
            "E4",
            "access_level_updated",
            is_active=True,
            access_level_id="premium",
        ),
        headers=AUTH,
    )
    status = await session.scalar(
        text("SELECT status FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert status == "active"
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS


async def test_unknown_event_type_is_ignored(client: AsyncClient, session: AsyncSession) -> None:
    user_id = await seed_user(session, device_id=DEVICE)
    response = await client.post(WEBHOOK, json=event("E5", "some_new_platform_event"), headers=AUTH)
    assert response.json() == {"result": "ignored", "reason": "unknown_event_type"}
    assert await balance_of(session, user_id) == 0
    assert int(await session.scalar(text("SELECT count(*) FROM payments")) or 0) == 0


# --- fail-closed products ---
async def test_granting_event_with_an_unknown_product_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE)
    response = await client.post(
        WEBHOOK,
        json=event("E6", "subscription_started", product_id="never.configured"),
        headers=AUTH,
    )

    assert response.json() == {"result": "rejected", "reason": "unknown_product"}
    assert await balance_of(session, user_id) == 0
    status = await session.scalar(
        text("SELECT status FROM payments WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert status == "rejected"
    # ACCESS FOLLOWS THE JOURNAL: a payment we refused to credit must not hand out a subscription.
    subscription = await session.scalar(
        text("SELECT count(*) FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert int(subscription or 0) == 0


async def test_product_of_another_channel_is_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, device_id=DEVICE)
    response = await client.post(
        WEBHOOK,
        json=event("E7", "subscription_started", product_id=PRODUCT_SUB_APPLE_ONLY),
        headers=AUTH,
    )
    assert response.json() == {"result": "rejected", "reason": "product_not_in_channel"}
    assert await balance_of(session, user_id) == 0


async def test_granting_event_without_a_transaction_id_is_lost_money_not_a_routine_no_grant(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The incident class: the subscription IS paid, but no grant key can be built — and
    never will be. It must be REJECTED (loud), not filed as a benign ``no_grant``."""
    user_id = await seed_user(session, device_id=DEVICE)
    response = await client.post(
        WEBHOOK,
        json=event("E8", "subscription_started", transaction_id=None),
        headers=AUTH,
    )

    assert response.json() == {"result": "rejected", "reason": "missing_transaction_id"}
    assert await balance_of(session, user_id) == 0
    subscription = await session.scalar(
        text("SELECT count(*) FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert int(subscription or 0) == 0  # access follows a grant we could not make


# --- garbage payloads: 200, never a retry storm ---
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"", "empty_body"),
        (b"{not json", "invalid_json"),
        (b"[1,2,3]", "not_an_object"),
        (b'{"event_type": "subscription_started"}', "missing_event_id"),
        (
            b'{"profile_event_id": "E9", "event_type": "subscription_started"}',
            "missing_customer_user_id",
        ),
    ],
)
async def test_malformed_payloads_are_ignored_with_a_machine_reason(
    client: AsyncClient, body: bytes, reason: str
) -> None:
    response = await client.post(WEBHOOK, content=body, headers=AUTH)
    assert response.status_code == 200  # a 4xx/5xx would make Adapty retry forever
    assert response.json()["reason"] == reason
