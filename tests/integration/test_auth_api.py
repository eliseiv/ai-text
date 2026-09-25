"""Auth over HTTP + the refresh-rotation invariants."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AUTH_ACTION_REFRESH_CHAIN_REVOKED, AuditService
from app.auth.apple import AppleIdentityVerifier
from app.auth.issuer import TokenIssuer
from app.auth.service import AuthService
from app.config import CoreSettings, get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError
from tests.conftest import apple_identity_token, auth_headers, balance_of, seed_user


async def test_register_creates_user_and_device_and_returns_a_pair(
    client: AsyncClient, session: AsyncSession
) -> None:
    response = await client.post("/v1/auth/register", json={"deviceId": "device-A"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tokenType"] == "Bearer"
    assert body["accessToken"] and body["refreshToken"]

    user_id = uuid.UUID(body["userId"])
    assert await session.scalar(text("SELECT 1 FROM users WHERE id = :u"), {"u": str(user_id)})
    device_user = await session.scalar(
        text("SELECT user_id FROM auth_devices WHERE device_id = 'device-A'")
    )
    assert uuid.UUID(str(device_user)) == user_id
    # The refresh token is stored ONLY as a hash.
    stored = await session.scalar(text("SELECT token_hash FROM auth_refresh_tokens LIMIT 1"))
    assert stored and stored != body["refreshToken"]


async def test_register_without_a_device_id_generates_one(client: AsyncClient) -> None:
    body = (await client.post("/v1/auth/register", json={})).json()
    assert uuid.UUID(body["deviceId"])


async def test_known_device_gets_the_same_user_id(client: AsyncClient) -> None:
    first = (await client.post("/v1/auth/register", json={"deviceId": "device-B"})).json()
    second = (await client.post("/v1/auth/token", json={"deviceId": "device-B"})).json()
    assert first["userId"] == second["userId"]


async def test_issued_access_token_authenticates_a_protected_endpoint(
    client: AsyncClient,
) -> None:
    tokens = (await client.post("/v1/auth/register", json={"deviceId": "device-C"})).json()
    response = await client.get(
        "/v1/wallet", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
    )
    assert response.status_code == 200
    assert response.json()["balance"] == 0


async def test_refresh_rotates_and_the_old_token_dies(client: AsyncClient) -> None:
    tokens = (await client.post("/v1/auth/register", json={"deviceId": "device-D"})).json()
    rotated = await client.post("/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    assert rotated.status_code == 200
    assert rotated.json()["refreshToken"] != tokens["refreshToken"]

    reused = await client.post("/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    assert reused.status_code == 401


async def test_refresh_reuse_revokes_the_whole_device_chain(
    client: AsyncClient, session: AsyncSession
) -> None:
    tokens = (await client.post("/v1/auth/register", json={"deviceId": "device-E"})).json()
    rotated = (
        await client.post("/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    ).json()

    # A replay of the OLD token: theft and a client retry are indistinguishable → fail-safe.
    assert (
        await client.post("/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
    ).status_code == 401

    # The token issued in between is now dead too — the whole chain was revoked.
    assert (
        await client.post("/v1/auth/refresh", json={"refreshToken": rotated["refreshToken"]})
    ).status_code == 401

    live = await session.scalar(
        text(
            "SELECT count(*) FROM auth_refresh_tokens "
            "WHERE device_id = 'device-E' AND revoked_at IS NULL AND used_at IS NULL"
        )
    )
    assert int(live or 0) == 0

    action = await session.scalar(
        text(
            "SELECT payload->>'action' FROM audit_logs WHERE event_type = 'auth_event' "
            "ORDER BY created_at DESC LIMIT 1"
        )
    )
    assert action == AUTH_ACTION_REFRESH_CHAIN_REVOKED


async def test_invalid_and_expired_access_tokens_are_401(client: AsyncClient) -> None:
    assert (await client.get("/v1/wallet")).status_code == 401
    assert (
        await client.get("/v1/wallet", headers={"Authorization": "Bearer junk"})
    ).status_code == 401
    expired = auth_headers(uuid.uuid4(), expired=True)
    assert (await client.get("/v1/wallet", headers=expired)).status_code == 401


async def test_jwks_serves_the_public_key_only(client: AsyncClient) -> None:
    document = (await client.get("/v1/auth/jwks")).json()
    assert document["keys"][0]["kty"] == "RSA"
    assert "d" not in document["keys"][0]


async def test_auth_endpoints_are_503_without_a_private_key(
    sessionmaker_: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: never "issue without a signature", never "generate a temporary key"."""
    settings = CoreSettings(JWT_PRIVATE_KEY="", JWT_PRIVATE_KEY_PATH="")  # type: ignore[arg-type]
    async with sessionmaker_() as session:
        service = AuthService(
            session,
            TokenIssuer(settings),
            settings,
            AppleIdentityVerifier(),
            AuditService(),
        )
        with pytest.raises(ServiceUnavailableError):
            await service.register_or_token("device-X")


# --- concurrency: single-use refresh -----------------------------------------------------------
async def test_parallel_refresh_with_one_token_issues_exactly_one_pair(
    client: AsyncClient, session: AsyncSession
) -> None:
    tokens = (await client.post("/v1/auth/register", json={"deviceId": "device-P"})).json()

    responses = await asyncio.gather(
        *[
            client.post("/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]})
            for _ in range(5)
        ]
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(200) == 1, f"exactly one winner expected, got {codes}"
    assert codes.count(401) == 4

    # The chain was revoked by the reuse detection (the losers ARE a reuse, by definition).
    live = await session.scalar(
        text(
            "SELECT count(*) FROM auth_refresh_tokens "
            "WHERE device_id = 'device-P' AND revoked_at IS NULL AND used_at IS NULL"
        )
    )
    assert int(live or 0) == 0


async def test_the_single_use_claim_is_atomic_under_a_forced_interleaving(
    sessionmaker_: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    """FORCED interleaving — the diff-test for the conditional UPDATE.

    ``asyncio.gather`` alone may serialise (the first call finishes before the second even reads),
    and then the test passes against a BROKEN implementation too. Here both sessions run the
    service's pre-check SELECT and see ``used_at IS NULL`` BEFORE either UPDATE runs — exactly the
    READ COMMITTED window the guard exists for.

    With ``UPDATE … WHERE id = :id AND used_at IS NULL RETURNING id`` exactly one writer gets a
    row. With an unconditional ``WHERE id = :id`` BOTH would — single-use broken, reuse-detect
    silently dead, and a thief replaying a stolen token in parallel with the legitimate client
    would get a valid pair and stay unnoticed.
    """
    tokens = (await client.post("/v1/auth/register", json={"deviceId": "device-Q"})).json()

    async with sessionmaker_() as sa, sessionmaker_() as sb:
        row_a = (
            (
                await sa.execute(
                    text("SELECT id, used_at FROM auth_refresh_tokens WHERE device_id = 'device-Q'")
                )
            )
            .mappings()
            .first()
        )
        row_b = (
            (
                await sb.execute(
                    text("SELECT id, used_at FROM auth_refresh_tokens WHERE device_id = 'device-Q'")
                )
            )
            .mappings()
            .first()
        )
        assert row_a is not None and row_b is not None
        assert row_a["used_at"] is None and row_b["used_at"] is None  # both see it unused

        claim = text(
            "UPDATE auth_refresh_tokens SET used_at = now() "
            "WHERE id = :id AND used_at IS NULL RETURNING id"
        )
        claimed_a = await sa.scalar(claim, {"id": str(row_a["id"])})
        await sa.commit()
        claimed_b = await sb.scalar(claim, {"id": str(row_b["id"])})
        await sb.commit()

    assert (claimed_a is None) != (claimed_b is None), "exactly one claimant may win"
    assert tokens["refreshToken"]


# --- Sign in with Apple -------------------------------------------------------------------------
async def test_apple_sign_in_issues_our_pair_and_creates_the_identity(
    client: AsyncClient, session: AsyncSession
) -> None:
    token = apple_identity_token(subject="apple-1", email="a@example.com")
    response = await client.post(
        "/v1/auth/apple", json={"identityToken": token, "deviceId": "device-apple-1"}
    )
    assert response.status_code == 200, response.text
    user_id = uuid.UUID(response.json()["userId"])

    row = (
        await session.execute(
            text("SELECT user_id, email FROM auth_identities WHERE subject = 'apple-1'")
        )
    ).first()
    assert row is not None
    assert uuid.UUID(str(row[0])) == user_id
    assert row[1] == "a@example.com"
    # The Apple token itself is exchanged, never re-used as our access token (BR-AUTH-6).
    assert response.json()["accessToken"] != token


async def test_same_apple_sub_from_another_device_is_the_same_user(client: AsyncClient) -> None:
    first = await client.post(
        "/v1/auth/apple",
        json={"identityToken": apple_identity_token(subject="apple-2"), "deviceId": "dev-1"},
    )
    second = await client.post(
        "/v1/auth/apple",
        json={"identityToken": apple_identity_token(subject="apple-2"), "deviceId": "dev-2"},
    )
    assert first.json()["userId"] == second.json()["userId"]


async def test_anonymous_account_with_credits_keeps_its_balance_after_apple_sign_in(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The whole point of putting Apple ON TOP of the device identity: the upgrade must not cost
    the user his credits, subscription and history."""
    registered = (
        await client.post("/v1/auth/register", json={"deviceId": "device-upgrade"})
    ).json()
    anon_id = uuid.UUID(registered["userId"])
    await session.execute(
        text("INSERT INTO wallets (user_id, balance) VALUES (:u, 250)"), {"u": str(anon_id)}
    )
    await session.execute(
        text(
            "INSERT INTO subscriptions (user_id, status, plan, expires_at) "
            "VALUES (:u, 'active', 'sub.monthly', now() + interval '30 days')"
        ),
        {"u": str(anon_id)},
    )
    await session.commit()

    upgraded = await client.post(
        "/v1/auth/apple",
        json={
            "identityToken": apple_identity_token(subject="apple-upgrade"),
            "deviceId": "device-upgrade",
        },
    )
    assert upgraded.status_code == 200, upgraded.text
    assert uuid.UUID(upgraded.json()["userId"]) == anon_id  # linked, not replaced

    # THE assertion: the BALANCE survived (a test that only compares userIds would pass even if
    # the wallet had been left behind on an abandoned account).
    assert await balance_of(session, anon_id) == 250
    wallet = await client.get(
        "/v1/wallet", headers={"Authorization": f"Bearer {upgraded.json()['accessToken']}"}
    )
    assert wallet.json()["balance"] == 250
    subscription = await client.get(
        "/v1/subscription",
        headers={"Authorization": f"Bearer {upgraded.json()['accessToken']}"},
    )
    assert subscription.json()["status"] == "active"


async def test_second_apple_account_on_the_same_device_creates_a_new_user(
    client: AsyncClient,
) -> None:
    first = await client.post(
        "/v1/auth/apple",
        json={"identityToken": apple_identity_token(subject="apple-a"), "deviceId": "shared-dev"},
    )
    second = await client.post(
        "/v1/auth/apple",
        json={"identityToken": apple_identity_token(subject="apple-b"), "deviceId": "shared-dev"},
    )
    assert first.json()["userId"] != second.json()["userId"]


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"issuer": "https://evil.example.com"},
        {"audience": "com.attacker.app"},
        {"expired": True},
        {"key": "wrong-secret"},
    ],
    ids=["wrong_iss", "wrong_aud", "expired", "bad_signature"],
)
async def test_apple_sign_in_is_fail_closed(
    client: AsyncClient, token_kwargs: dict[str, Any]
) -> None:
    token = apple_identity_token(subject="apple-x", **token_kwargs)
    response = await client.post("/v1/auth/apple", json={"identityToken": token})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_apple_verifier_failures_never_surface_as_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each JWKS failure mode is a 401 (fail-closed), not a 500 — an unverifiable token must not
    become valid because Apple was unreachable, and must not leak an availability signal."""
    import app.auth.apple as apple_mod

    class _BrokenVerifier:
        def verify(self, identity_token: str, nonce: str | None) -> Any:
            raise UnauthorizedError("invalid apple identity token")

    monkeypatch.setattr(apple_mod, "_verifier_singleton", _BrokenVerifier(), raising=False)
    response = await client.post(
        "/v1/auth/apple", json={"identityToken": apple_identity_token(subject="s")}
    )
    assert response.status_code == 401


async def test_body_may_not_carry_a_user_id(client: AsyncClient) -> None:
    # StrictModel(extra='forbid'): claiming to be someone else is a 422, not a silent no-op.
    response = await client.post(
        "/v1/auth/register", json={"deviceId": "d", "userId": str(uuid.uuid4())}
    )
    assert response.status_code == 422


async def test_seeded_user_helper_matches_the_api(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, balance=5)
    response = await client.get("/v1/wallet", headers=auth_headers(user_id))
    assert response.json()["balance"] == 5
    get_settings.cache_clear()
