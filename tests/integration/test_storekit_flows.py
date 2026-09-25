"""StoreKit flows: subscription sync + consumable token purchase.

The verifier is faked at the boundary (its own cryptography is proven in
``tests/unit/test_storekit_verifier.py`` with REAL certificate chains). What is proven here is
everything the CLIENT must not control: the productId, the term, the amount and the addressee.
"""

from __future__ import annotations

import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import InvalidTransactionError, VerificationUnavailableError
from tests.conftest import (
    PRODUCT_SUB,
    PRODUCT_SUB_CREDITS,
    PRODUCT_TOKENS,
    PRODUCT_TOKENS_CREDITS,
    FakeStoreKitVerifier,
    auth_headers,
    balance_of,
    seed_user,
)


# --- subscription sync ---------------------------------------------------------------------------
async def test_sync_activates_and_grants_the_period_credits(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session)
    fake_storekit.script(transaction_id="apple-txn-1", product_id=PRODUCT_SUB)

    response = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["plan"] == PRODUCT_SUB
    assert body["creditsGranted"] == PRODUCT_SUB_CREDITS
    assert body["newBalance"] == PRODUCT_SUB_CREDITS
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS

    row = (
        await session.execute(
            text(
                "SELECT channel, external_id, status, credits_granted, grant_idempotency_key "
                "FROM payments WHERE user_id = :u"
            ),
            {"u": str(user_id)},
        )
    ).first()
    assert row is not None
    assert row[0] == "apple_storekit"
    assert row[1] == "apple-txn-1"
    assert row[2] == "granted"
    assert int(row[3]) == PRODUCT_SUB_CREDITS
    assert row[4] == "sub-grant:apple-txn-1"


async def test_restore_purchases_does_not_credit_twice(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session)
    fake_storekit.script(transaction_id="apple-txn-2", product_id=PRODUCT_SUB)

    first = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    second = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )

    assert first.json()["creditsGranted"] == PRODUCT_SUB_CREDITS
    assert second.json()["creditsGranted"] == 0
    assert second.json()["idempotentReplay"] is True
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS


async def test_expired_transaction_syncs_as_expired(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session)
    fake_storekit.script(transaction_id="apple-txn-3", product_id=PRODUCT_SUB, expires_in_days=-1)
    response = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    assert response.json()["status"] == "expired"


async def test_revoked_transaction_is_not_active(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session)
    fake_storekit.script(transaction_id="apple-txn-4", product_id=PRODUCT_SUB, revoked=True)
    response = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    assert response.json()["status"] == "expired"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (InvalidTransactionError("forged"), "invalid_transaction"),
        (VerificationUnavailableError("no root ca"), "verification_unavailable"),
    ],
)
async def test_verification_failures_are_422_and_credit_nothing(
    client: AsyncClient,
    session: AsyncSession,
    fake_storekit: FakeStoreKitVerifier,
    error: Exception,
    code: str,
) -> None:
    user_id = await seed_user(session)
    fake_storekit.error = error

    response = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert await balance_of(session, user_id) == 0
    assert int(await session.scalar(text("SELECT count(*) FROM payments")) or 0) == 0


async def test_unknown_product_is_422_with_no_fallback_grant(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session)
    fake_storekit.script(transaction_id="apple-txn-5", product_id="never.configured")

    response = await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_product"
    assert await balance_of(session, user_id) == 0  # NO default amount, ever (BR-8)


async def test_subscription_status_reflects_lazy_expiry(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    await session.execute(
        text(
            "INSERT INTO subscriptions (user_id, status, plan, expires_at) "
            "VALUES (:u, 'active', 'sub.monthly', :exp)"
        ),
        {
            "u": str(user_id),
            "exp": datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=1),
        },
    )
    await session.commit()

    # The stored column still says 'active'; the API must report what the POLICY will see.
    stored = await session.scalar(
        text("SELECT status FROM subscriptions WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert stored == "active"

    response = await client.get("/v1/subscription", headers=auth_headers(user_id))
    assert response.json()["status"] == "expired"


# --- consumable token purchase ---
async def test_token_purchase_requires_an_active_subscription(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    """The guard runs AFTER verification ⇒ Apple already took the money ⇒ this 403 is a
    REFUND-NEEDED event, not a routine validation error."""
    user_id = await seed_user(session)
    fake_storekit.script(transaction_id="apple-tokens-1", product_id=PRODUCT_TOKENS)

    response = await client.post(
        "/v1/tokens/purchase", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "subscription_required"
    assert await balance_of(session, user_id) == 0
    assert fake_storekit.calls == 1  # the verifier DID run before the refusal


async def test_token_purchase_credits_from_the_server_side_map(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session, subscription="active", balance=0)
    fake_storekit.script(transaction_id="apple-tokens-2", product_id=PRODUCT_TOKENS)

    response = await client.post(
        "/v1/tokens/purchase", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["creditsAdded"] == PRODUCT_TOKENS_CREDITS
    assert body["productId"] == PRODUCT_TOKENS  # from the VERIFIED payload, not from the body
    assert await balance_of(session, user_id) == PRODUCT_TOKENS_CREDITS

    key = await session.scalar(
        text("SELECT grant_idempotency_key FROM payments WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert key == "token-purchase:apple-tokens-2"  # namespaced: a consumable and a subscription
    # may share an Apple id, and without the prefix the second would silently credit nothing.


async def test_token_purchase_replay_credits_once(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    user_id = await seed_user(session, subscription="active", balance=0)
    fake_storekit.script(transaction_id="apple-tokens-3", product_id=PRODUCT_TOKENS)

    await client.post(
        "/v1/tokens/purchase", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )
    second = await client.post(
        "/v1/tokens/purchase", json={"transaction": "jws"}, headers=auth_headers(user_id)
    )

    assert second.json()["creditsAdded"] == 0
    assert second.json()["idempotentReplay"] is True
    assert await balance_of(session, user_id) == PRODUCT_TOKENS_CREDITS


async def test_purchase_body_cannot_carry_credits_or_product(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session, subscription="active")
    for payload in (
        {"transaction": "jws", "credits": 999999},
        {"transaction": "jws", "productId": "tokens.100"},
        {"transaction": "jws", "userId": str(user_id)},
    ):
        response = await client.post(
            "/v1/tokens/purchase", json=payload, headers=auth_headers(user_id)
        )
        assert response.status_code == 422  # extra='forbid' — the field does not exist


async def test_grant_namespaces_keep_a_subscription_and_a_consumable_apart(
    session: AsyncSession,
) -> None:
    """The ledger key is NAMESPACED (`sub-grant:` / `token-purchase:`).

    Without the prefix, two grants sharing one Apple id would collapse into a single ledger key and
    the second would silently credit nothing. (Layer 1 — `payments UNIQUE(channel, external_id)` —
    is a DIFFERENT key and dedups DELIVERIES; Apple never reuses a transactionId, so the two never
    meet there.)
    """
    from app.audit.service import AuditService
    from app.wallet.service import WalletService

    user_id = await seed_user(session)
    wallet = WalletService(session, AuditService())

    await wallet.grant(
        user_id=user_id,
        amount=PRODUCT_SUB_CREDITS,
        idempotency_key="sub-grant:shared-id",
        reason="apple_storekit_subscription",
    )
    await wallet.grant(
        user_id=user_id,
        amount=PRODUCT_TOKENS_CREDITS,
        idempotency_key="token-purchase:shared-id",
        reason="apple_storekit_tokens",
    )
    await session.commit()

    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS + PRODUCT_TOKENS_CREDITS
    keys = (
        await session.execute(
            text(
                "SELECT idempotency_key FROM ledger_transactions WHERE user_id = :u "
                "ORDER BY idempotency_key"
            ),
            {"u": str(user_id)},
        )
    ).all()
    assert [k[0] for k in keys] == ["sub-grant:shared-id", "token-purchase:shared-id"]


async def test_payments_history_is_owner_scoped(
    client: AsyncClient, session: AsyncSession, fake_storekit: FakeStoreKitVerifier
) -> None:
    owner = await seed_user(session)
    stranger = await seed_user(session)
    fake_storekit.script(transaction_id="apple-hist", product_id=PRODUCT_SUB)
    await client.post(
        "/v1/subscription/sync", json={"transaction": "jws"}, headers=auth_headers(owner)
    )

    mine = await client.get("/v1/payments", headers=auth_headers(owner))
    theirs = await client.get("/v1/payments", headers=auth_headers(stranger))

    assert len(mine.json()["items"]) == 1
    assert theirs.json()["items"] == []
    assert "payload" not in mine.json()["items"][0]  # internals never travel outward


async def test_products_catalogue_is_served_from_the_same_map(
    client: AsyncClient, session: AsyncSession
) -> None:
    user_id = await seed_user(session)
    response = await client.get("/v1/products", headers=auth_headers(user_id))
    products = {p["productId"]: p for p in response.json()["products"]}
    assert products[PRODUCT_SUB]["credits"] == PRODUCT_SUB_CREDITS
    assert products[PRODUCT_TOKENS]["credits"] == PRODUCT_TOKENS_CREDITS

    filtered = await client.get("/v1/products?channel=adapty", headers=auth_headers(user_id))
    assert {p["productId"] for p in filtered.json()["products"]} == {PRODUCT_SUB}
