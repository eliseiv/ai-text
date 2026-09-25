"""Gateway (size limit, headers, isolation, rate limits), admin, profile, checkout, migrations."""

from __future__ import annotations

import datetime
import json
import logging
import uuid

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import (
    ADMIN_SECRET,
    CLOUDPAYMENTS_API_BASE,
    PRODUCT_SUB,
    PRODUCT_SUB_APPLE_ONLY,
    auth_headers,
    balance_of,
    deny_limiter,
    seed_user,
)

ADMIN = {"X-Admin-Token": ADMIN_SECRET}


# --- gateway ------------------------------------------------------------------------------------
async def test_oversized_body_is_413_before_parsing(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    response = await client.post(
        "/v1/generate",
        content=b"x" * (600 * 1024),  # SIZE_LIMIT_BODY = 512 KB
        headers={**auth_headers(user_id), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_security_headers_and_request_id(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" in response.headers
    assert response.headers["X-Request-Id"]

    echoed = await client.get("/health", headers={"X-Request-Id": "rid-123"})
    assert echoed.headers["X-Request-Id"] == "rid-123"


async def test_admin_token_does_not_authorize_user_endpoints(client: AsyncClient) -> None:
    response = await client.get("/v1/wallet", headers=ADMIN)
    assert response.status_code == 401


async def test_user_jwt_does_not_authorize_admin_endpoints(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    response = await client.get(f"/v1/admin/wallet/{user_id}", headers=auth_headers(user_id))
    assert response.status_code == 401  # not a 403: no code on this path even reads a JWT


async def test_rate_limited_requests_are_429(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    deny_limiter(monkeypatch, "enforce_generation_limits")
    response = await client.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


async def test_secrets_never_appear_in_the_logs(
    client: AsyncClient, session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user_id = await seed_user(session, subscription="active", balance=10)
    with caplog.at_level(logging.DEBUG):
        await client.get(
            "/v1/wallet",
            headers={**auth_headers(user_id), "X-Admin-Token": ADMIN_SECRET},
        )
    dumped = json.dumps(
        [{"msg": r.getMessage(), "fields": getattr(r, "extra_fields", {})} for r in caplog.records],
        default=str,
    )
    assert ADMIN_SECRET not in dumped
    assert "Bearer " not in dumped


async def test_health_and_ready_shape(client: AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/healthz")).json() == {"status": "ok"}

    ready = await client.get("/ready")
    # CONTRACT only: the real availability of the infrastructure belongs to an e2e run against a
    # deployed stack, not to a hermetic test (Redis is deliberately absent here).
    assert ready.status_code in (200, 503)
    body = ready.json()
    assert set(body) == {"db", "redis"}
    assert body["db"] in {"ok", "down"} and body["redis"] in {"ok", "down"}


# --- admin --------------------------------------------------------------------------------------
async def test_admin_grant_is_idempotent_and_audited(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    payload = {
        "userId": str(user_id),
        "credits": 100,
        "idempotencyKey": "support-ticket-1234",
        "reason": "compensation",
    }

    first = await client.post("/v1/admin/wallet/grant", json=payload, headers=ADMIN)
    second = await client.post("/v1/admin/wallet/grant", json=payload, headers=ADMIN)

    assert first.json()["creditsGranted"] == 100
    assert second.json()["creditsGranted"] == 0
    assert second.json()["idempotentReplay"] is True
    assert await balance_of(session, user_id) == 100

    # The idempotencyKey is the ONLY attribution thread of a manual grant — it MUST survive
    # redaction (a support ticket is not a secret).
    payload_row = await session.scalar(
        text("SELECT payload FROM audit_logs WHERE event_type = 'admin_grant' LIMIT 1")
    )
    assert payload_row["idempotencyKey"] == "support-ticket-1234"


async def test_admin_grant_with_the_same_key_and_another_amount_is_409(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    base = {"userId": str(user_id), "credits": 10, "idempotencyKey": "k"}
    await client.post("/v1/admin/wallet/grant", json=base, headers=ADMIN)
    conflict = await client.post(
        "/v1/admin/wallet/grant", json={**base, "credits": 20}, headers=ADMIN
    )
    assert conflict.status_code == 409


async def test_admin_never_creates_users(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uuid.uuid4()), "credits": 10, "idempotencyKey": "k"},
        headers=ADMIN,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"


async def test_admin_subscription_grant_restores_access(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Credits alone are NOT enough: the subscription is the RIGHT, credits are the RESOURCE."""
    user_id = await seed_user(session, trial_used=True)
    response = await client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(user_id), "days": 30, "idempotencyKey": "sub-k"},
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert response.json()["creditsGranted"] == 1000  # SUBSCRIPTION_CREDITS_PER_PERIOD

    effective = await client.get("/v1/policy/effective", headers=auth_headers(user_id))
    assert effective.json()["allowed"] is True  # the user can actually generate again


async def test_admin_subscription_grant_rejects_a_past_expiry(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    past = (datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1)).isoformat()
    response = await client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(user_id), "expiresAt": past, "idempotencyKey": "k"},
        headers=ADMIN,
    )
    assert response.status_code == 422  # a grant that expires in the past is useless


async def test_admin_grant_namespaces_do_not_collide(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    await client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(user_id), "credits": 10, "idempotencyKey": "same"},
        headers=ADMIN,
    )
    await client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(user_id), "days": 30, "credits": 20, "idempotencyKey": "same"},
        headers=ADMIN,
    )
    assert await balance_of(session, user_id) == 30  # both grants landed


async def test_admin_wallet_view(client: AsyncClient, session: AsyncSession) -> None:
    user_id = await seed_user(session, balance=42)
    response = await client.get(f"/v1/admin/wallet/{user_id}", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["balance"] == 42


# --- profile ------------------------------------------------------------------------------------
async def test_profile_defaults_and_update(client: AsyncClient, session: AsyncSession) -> None:
    user_id = await seed_user(session)
    initial = await client.get("/v1/profile", headers=auth_headers(user_id))
    assert initial.status_code == 200  # a missing profile row is not a 404
    assert initial.json()["displayName"] is None
    account_id = initial.json()["accountId"]
    assert len(account_id.split("-")) == 3

    updated = await client.patch(
        "/v1/profile", json={"displayName": "Ada"}, headers=auth_headers(user_id)
    )
    assert updated.json()["displayName"] == "Ada"
    assert updated.json()["accountId"] == account_id  # derived, stable, never stored

    cleared = await client.patch(
        "/v1/profile", json={"displayName": None}, headers=auth_headers(user_id)
    )
    assert cleared.json()["displayName"] is None


def test_account_id_is_deterministic_and_dictation_safe() -> None:
    from app.profile.account_id import account_id

    user_id = uuid.uuid4()
    assert account_id(user_id) == account_id(user_id)
    assert not set("IO01") & set(account_id(user_id).replace("-", "")[8:])


# --- RU checkout --------------------------------------------------------------------------------
@respx.mock
async def test_checkout_sends_the_user_id_from_the_token(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The client used to build the link itself with a client-controlled user_id (lost payments)
    and the aggregator token in the binary. Now both live on the server."""
    user_id = await seed_user(session)
    route = respx.post(f"{CLOUDPAYMENTS_API_BASE}/payments/link").mock(
        return_value=httpx.Response(
            200,
            json={
                "payment_id": "p-1",
                "payment_url": "https://pay.example.test/p/1",
                "status": "created",
            },
        )
    )

    response = await client.post(
        "/v1/billing/cloudpayments/checkout",
        json={"productId": PRODUCT_SUB, "customerEmail": "buyer@example.com"},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200, response.text
    assert response.json()["paymentUrl"] == "https://pay.example.test/p/1"
    sent = route.calls[0].request.content.decode()
    assert str(user_id) in sent  # from the JWT sub…
    assert "app-1" in sent  # …and the app id never leaves the server


async def test_checkout_body_cannot_carry_a_user_id_or_amount(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    for payload in (
        {"productId": PRODUCT_SUB, "customerEmail": "a@b.c", "userId": str(user_id)},
        {"productId": PRODUCT_SUB, "customerEmail": "a@b.c", "amount": 1},
    ):
        response = await client.post(
            "/v1/billing/cloudpayments/checkout", json=payload, headers=auth_headers(user_id)
        )
        assert response.status_code == 422


async def test_checkout_rejects_a_product_of_another_channel(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    response = await client.post(
        "/v1/billing/cloudpayments/checkout",
        json={"productId": PRODUCT_SUB_APPLE_ONLY, "customerEmail": "a@b.c"},
        headers=auth_headers(user_id),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "product_not_in_channel"


@respx.mock
async def test_checkout_upstream_failure_is_a_502_that_leaks_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    respx.post(f"{CLOUDPAYMENTS_API_BASE}/payments/link").mock(
        return_value=httpx.Response(500, text="upstream internals with a token")
    )
    response = await client.post(
        "/v1/billing/cloudpayments/checkout",
        json={"productId": PRODUCT_SUB, "customerEmail": "a@b.c"},
        headers=auth_headers(user_id),
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    assert "token" not in response.text


async def test_checkout_is_503_when_the_channel_is_not_configured(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", "")
    get_settings.cache_clear()
    try:
        user_id = await seed_user(session)
        response = await client.post(
            "/v1/billing/cloudpayments/checkout",
            json={"productId": PRODUCT_SUB, "customerEmail": "a@b.c"},
            headers=auth_headers(user_id),
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "cloudpayments_checkout_not_configured"
    finally:
        get_settings.cache_clear()
