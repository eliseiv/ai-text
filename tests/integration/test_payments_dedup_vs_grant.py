"""THE two mandatory billing tests.

Both are DIFF-TESTS: each fails against the pre-fix implementation, which is the only thing that
proves they lock anything at all.

* ``test_payments_dedup_vs_grant`` — key the grant on the EVENT id and the balance doubles,
  silently. One purchase = several Adapty events = ONE transaction id.
* ``test_record_and_grant_atomicity`` — commit layer 1 on its own and a crash between the layers
  loses the payment FOREVER (the retry sees "already delivered" and grants nothing).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.billing.payments import KIND_SUBSCRIPTION, PaymentsJournal
from app.wallet.service import WalletService
from tests.conftest import (
    ADAPTY_SECRET,
    PRODUCT_SUB,
    PRODUCT_SUB_CREDITS,
    balance_of,
    ledger_rows,
    seed_user,
)

DEVICE = "DEVICE-ADAPTY-1"


def _adapty_event(event_id: str, transaction_id: str, event_type: str) -> dict[str, Any]:
    """A REAL-shaped Adapty payload: ids under ``event_properties``, ``profile_event_id`` on top."""
    return {
        "profile_event_id": event_id,
        "event_type": event_type,
        "customer_user_id": DEVICE.lower(),  # the client sends its own casing — resolve is CI
        "event_properties": {
            "vendor_product_id": PRODUCT_SUB,
            "transaction_id": transaction_id,
            "original_transaction_id": "orig-1",
            "subscription_expires_at": "2035-01-01T00:00:00Z",
        },
    }


async def test_payments_dedup_vs_grant(client: AsyncClient, session: AsyncSession) -> None:
    """Two events, DIFFERENT ``profile_event_id``, ONE ``transaction_id``.

    Expected: 2 rows in ``payments`` (the second ``replayed`` at layer ``grant``, 0 credits) and
    exactly 1 row in ``ledger_transactions``. The balance is credited ONCE.

    DIFF: key the grant on ``profile_event_id`` and this test fails — two ledger rows, double
    balance. That is the bug this file exists for.
    """
    user_id = await seed_user(session, device_id=DEVICE)

    headers = {"Authorization": f"Bearer {ADAPTY_SECRET}"}
    first = await client.post(
        "/v1/billing/adapty/webhook",
        json=_adapty_event("E1", "T1", "trial_started"),
        headers=headers,
    )
    second = await client.post(
        "/v1/billing/adapty/webhook",
        json=_adapty_event("E2", "T1", "subscription_started"),
        headers=headers,
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["result"] == "applied"
    assert second.json()["result"] == "duplicate"  # layer 2: the grant already happened

    payments = (
        await session.execute(
            text(
                "SELECT external_id, status, credits_granted, ledger_tx_id "
                "FROM payments WHERE user_id = :u ORDER BY external_id"
            ),
            {"u": str(user_id)},
        )
    ).all()
    assert len(payments) == 2, "both DELIVERIES must be journalled (layer 1 keys on the event id)"
    assert payments[0][0] == "E1" and payments[0][1] == "granted"
    assert int(payments[0][2]) == PRODUCT_SUB_CREDITS
    assert payments[1][0] == "E2" and payments[1][1] == "replayed"
    assert int(payments[1][2]) == 0
    assert payments[0][3] == payments[1][3]  # both point at the SAME ledger transaction

    ledger = await ledger_rows(session, user_id)
    assert len(ledger) == 1, "ONE grant per billing period (layer 2 keys on the transaction id)"
    assert ledger[0]["idempotency_key"] == "sub-grant:T1"  # shared with StoreKit sync
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS  # NOT doubled


async def test_repeat_of_the_same_webhook_is_a_delivery_replay(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The SAME event id twice: layer 1 stops it — no new row, no credits."""
    user_id = await seed_user(session, device_id=DEVICE)
    headers = {"Authorization": f"Bearer {ADAPTY_SECRET}"}
    body = _adapty_event("E1", "T1", "subscription_started")

    await client.post("/v1/billing/adapty/webhook", json=body, headers=headers)
    repeat = await client.post("/v1/billing/adapty/webhook", json=body, headers=headers)

    assert repeat.json()["result"] == "duplicate"
    assert repeat.json()["reason"] == "duplicate_delivery"
    count = await session.scalar(
        text("SELECT count(*) FROM payments WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert int(count or 0) == 1
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS


async def test_record_and_grant_atomicity(
    session: AsyncSession, sessionmaker_: async_sessionmaker[AsyncSession]
) -> None:
    """A failure BETWEEN layer 1 and layer 2 must roll BOTH back; the retry then grants ONCE.

    DIFF: add ``await session.commit()`` right after the INSERT into ``payments`` and this test
    fails — the row survives the crash, the retry sees "already delivered" (``ON CONFLICT DO
    NOTHING`` returns nothing) and grants NOTHING. The payment is lost forever, silently, and the
    row sits there with ``status='received'``.
    """
    user_id = await seed_user(session)

    class _ExplodingWallet(WalletService):
        async def grant(self, **kwargs: Any) -> Any:  # type: ignore[override]
            raise RuntimeError("injected failure between layer 1 and layer 2")

    # --- the crash ---
    async with sessionmaker_() as s:
        journal = PaymentsJournal(_ExplodingWallet(s, AuditService()), AuditService())
        with pytest.raises(RuntimeError):
            await journal.record_and_grant(
                session=s,
                channel="adapty",
                external_id="E-crash",
                user_id=user_id,
                product_id=PRODUCT_SUB,
                kind=KIND_SUBSCRIPTION,
                grant_idempotency_key="adapty-txn:T-crash",
            )
        await s.rollback()

    assert await _payments_count(session) == 0, "layer 1 must NOT survive on its own"
    assert await ledger_rows(session, user_id) == []
    assert await balance_of(session, user_id) == 0

    # --- the retry of the SAME webhook ---
    async with sessionmaker_() as s:
        journal = PaymentsJournal(WalletService(s, AuditService()), AuditService())
        outcome = await journal.record_and_grant(
            session=s,
            channel="adapty",
            external_id="E-crash",
            user_id=user_id,
            product_id=PRODUCT_SUB,
            kind=KIND_SUBSCRIPTION,
            grant_idempotency_key="adapty-txn:T-crash",
        )
        await s.commit()

    assert outcome.status == "granted"
    assert outcome.credits == PRODUCT_SUB_CREDITS
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS
    assert len(await ledger_rows(session, user_id)) == 1

    # --- and once more, for good measure: exactly ONE grant ever ---
    async with sessionmaker_() as s:
        journal = PaymentsJournal(WalletService(s, AuditService()), AuditService())
        again = await journal.record_and_grant(
            session=s,
            channel="adapty",
            external_id="E-crash",
            user_id=user_id,
            product_id=PRODUCT_SUB,
            kind=KIND_SUBSCRIPTION,
            grant_idempotency_key="adapty-txn:T-crash",
        )
        await s.commit()
    assert again.status == "replayed"
    assert again.credits == 0
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS


async def test_no_payment_row_is_ever_left_in_received(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The diagnostic invariant: ``received`` is the NEUTRAL start status. A row that stays in it
    is a payment that was journalled but never finished — and must not exist."""
    user_id = await seed_user(session, device_id=DEVICE)
    headers = {"Authorization": f"Bearer {ADAPTY_SECRET}"}

    for event_id, txn, event_type in [
        ("A1", "TA", "trial_started"),
        ("A2", "TA", "subscription_started"),
        ("A3", "TB", "subscription_renewed"),
        ("A4", "", "subscription_expired"),
        ("A5", "", "subscription_renewal_cancelled"),
    ]:
        body = _adapty_event(event_id, txn, event_type)
        await client.post("/v1/billing/adapty/webhook", json=body, headers=headers)

    stuck = await session.scalar(text("SELECT count(*) FROM payments WHERE status = 'received'"))
    assert int(stuck or 0) == 0
    assert user_id


async def _payments_count(session: AsyncSession) -> int:
    value = await session.scalar(text("SELECT count(*) FROM payments"))
    return int(value or 0)


async def test_amount_in_the_body_never_decides_the_credits(
    client: AsyncClient, session: AsyncSession
) -> None:
    """BR-8: the amount comes from the server-side PRODUCTS map, never from the callback body."""
    user_id = await seed_user(session, device_id=DEVICE)
    body = _adapty_event("E-amount", "T-amount", "subscription_started")
    body["event_properties"]["price"] = 999999  # type: ignore[index]
    body["credits"] = 999999

    await client.post(
        "/v1/billing/adapty/webhook",
        json=body,
        headers={"Authorization": f"Bearer {ADAPTY_SECRET}"},
    )
    assert await balance_of(session, user_id) == PRODUCT_SUB_CREDITS


async def test_unknown_user_creates_nothing(client: AsyncClient, session: AsyncSession) -> None:
    body = _adapty_event("E-nouser", "T-nouser", "subscription_started")
    body["customer_user_id"] = str(uuid.uuid4())

    response = await client.post(
        "/v1/billing/adapty/webhook",
        json=body,
        headers={"Authorization": f"Bearer {ADAPTY_SECRET}"},
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "user_not_found"
    assert int(await session.scalar(text("SELECT count(*) FROM users")) or 0) == 0
    assert await _payments_count(session) == 0
