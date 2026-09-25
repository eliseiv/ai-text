"""``resolve_user()`` — the class of bug that cost TWO production incidents.

⚠️ FIXTURE REQUIREMENT: the STORED ``device_id`` and the value the
webhook SENDS must differ in CASE. Payment aggregators forward the identifier in the casing the
client gave them (iOS ``identifierForVendor.uuidString`` is UPPERCASE, ``str(uuid.UUID)`` is
lowercase). A test where both sides already match cannot catch a normalisation bug at all — and
produces a false sense of coverage. That is precisely how the fix shipped to CloudPayments and
stayed broken in Adapty.
"""

from __future__ import annotations

import uuid

import httpx
import respx
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing_common.resolve import RESOLVED_VIA_DEVICE_ID, RESOLVED_VIA_USER_ID, resolve_user
from tests.conftest import (
    ADAPTY_SECRET,
    CLOUDPAYMENTS_API_BASE,
    PRODUCT_SUB,
    PRODUCT_SUB_CREDITS,
    balance_of,
    seed_user,
)

DEVICE_UPPER = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
DEVICE_LOWER = DEVICE_UPPER.lower()


async def test_identifier_is_our_user_id(session: AsyncSession) -> None:
    user_id = await seed_user(session)
    resolved = await resolve_user(session, str(user_id))
    assert resolved == (user_id, RESOLVED_VIA_USER_ID)


async def test_identifier_is_a_device_id(session: AsyncSession) -> None:
    user_id = await seed_user(session, device_id="plain-device")
    resolved = await resolve_user(session, "plain-device")
    assert resolved == (user_id, RESOLVED_VIA_DEVICE_ID)


async def test_stored_upper_query_lower(session: AsyncSession) -> None:
    """THE incident fixture: stored UPPER, queried lower."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)
    resolved = await resolve_user(session, DEVICE_LOWER)
    assert resolved == (user_id, RESOLVED_VIA_DEVICE_ID)


async def test_stored_lower_query_upper(session: AsyncSession) -> None:
    """…and the mirror image, because the casing depends on which client wrote the row."""
    user_id = await seed_user(session, device_id=DEVICE_LOWER)
    resolved = await resolve_user(session, DEVICE_UPPER)
    assert resolved == (user_id, RESOLVED_VIA_DEVICE_ID)


async def test_casing_collision_is_deterministic_and_never_500(session: AsyncSession) -> None:
    """Two devices differing ONLY by case (TD-008: the functional index is deliberately NOT
    unique). Without ``ORDER BY user_id LIMIT 1`` this raises ``MultipleResultsFound`` ON THE
    PAYMENT PATH → 500 → the aggregator retries → 500 again → the payment hangs forever."""
    first = await seed_user(session, device_id="ABC-COLLIDE")
    second = await seed_user(session, device_id="abc-collide")

    picks = {await resolve_user(session, "AbC-CoLlIdE") for _ in range(5)}
    assert len(picks) == 1, "the pick must be STABLE, not arbitrary — this is money"
    [pick] = picks
    assert pick is not None
    assert pick[0] in {first, second}
    assert pick[1] == RESOLVED_VIA_DEVICE_ID


async def test_registering_a_device_that_differs_only_by_case_succeeds(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The index is NOT unique on ``lower(device_id)``: registering ``ABC`` after ``abc`` is a new
    device, not an IntegrityError."""
    first = await client.post("/v1/auth/register", json={"deviceId": "case-device"})
    second = await client.post("/v1/auth/register", json={"deviceId": "CASE-DEVICE"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["userId"] != second.json()["userId"]
    count = await session.scalar(
        text("SELECT count(*) FROM auth_devices WHERE lower(device_id) = 'case-device'")
    )
    assert int(count or 0) == 2


async def test_unknown_identifier_resolves_to_nothing(session: AsyncSession) -> None:
    assert await resolve_user(session, str(uuid.uuid4())) is None
    assert await resolve_user(session, "who-is-this") is None
    assert await resolve_user(session, "   ") is None


async def test_garbage_identifier_does_not_break_the_money_path(session: AsyncSession) -> None:
    # A non-UUID must never produce a cast error inside a payment path.
    assert await resolve_user(session, "'; DROP TABLE users; --") is None


async def test_adapty_credits_a_device_sent_in_the_other_casing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """End-to-end through the REAL webhook: stored UPPER, Adapty sends lower → the payment lands."""
    user_id = await seed_user(session, device_id=DEVICE_UPPER)

    response = await client.post(
        "/v1/billing/adapty/webhook",
        json={
            "profile_event_id": "E-case",
            "event_type": "subscription_started",
            "customer_user_id": DEVICE_LOWER,
            "event_properties": {
                "vendor_product_id": PRODUCT_SUB,
                "transaction_id": "T-case",
            },
        },
        headers={"Authorization": f"Bearer {ADAPTY_SECRET}"},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "applied"
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS


@respx.mock
async def test_cloudpayments_credits_a_device_sent_in_the_other_casing(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Stored lower, the aggregator sends UPPER (that is what it actually does)."""
    user_id = await seed_user(session, device_id=DEVICE_LOWER)
    respx.get(f"{CLOUDPAYMENTS_API_BASE}/users/{DEVICE_UPPER}/payments").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "payment_id": "pay-case",
                        "status": "succeeded",
                        "paid_at": "2035-01-01T00:00:00Z",
                        "product": {"code": PRODUCT_SUB, "payment_type": "subscription"},
                    }
                ]
            },
        )
    )

    response = await client.post(
        "/v1/billing/cloudpayments/webhook",
        json={
            "Status": "Completed",
            "OperationType": "Payment",
            "AccountId": DEVICE_UPPER,
            "TransactionId": 1,
        },
    )

    assert response.status_code == 200
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS


def test_both_channels_use_the_one_shared_resolver() -> None:
    """No channel-local copy: the duplication is why the same fix had nowhere to travel and the
    incident happened a second time."""
    import inspect

    from app.billing_adapty import service as adapty_service
    from app.billing_cloudpayments import service as cp_service

    for module in (adapty_service, cp_service):
        source = inspect.getsource(module)
        assert "from app.billing_common.resolve import resolve_user" in source
        assert "auth_devices" not in source, "a channel-local resolve is forbidden"
