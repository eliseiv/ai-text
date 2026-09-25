"""Wallet / Ledger (AC-3) — against a REAL PostgreSQL: the invariants live there."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.errors import ConflictError, InsufficientCreditsError, ValidationFailedError
from app.wallet.service import WalletService
from tests.conftest import balance_of, ledger_rows, seed_user


def _wallet(session: AsyncSession) -> WalletService:
    return WalletService(session, AuditService())


async def test_grant_then_consume(session: AsyncSession) -> None:
    user_id = await seed_user(session)
    wallet = _wallet(session)

    granted = await wallet.grant(
        user_id=user_id, amount=100, idempotency_key="admin-grant:k1", reason="admin_grant"
    )
    assert granted.replay is False
    assert granted.balance == 100

    consumed = await wallet.consume(
        user_id=user_id, amount=30, idempotency_key="generation:g1", reason="generation"
    )
    assert consumed.replay is False
    assert consumed.balance == 70
    await session.commit()

    assert await balance_of(session, user_id) == 70
    rows = await ledger_rows(session, user_id)
    assert [(r["type"], r["amount"]) for r in rows] == [("credit", 100), ("debit", 30)]


async def test_repeated_grant_with_the_same_key_is_idempotent(session: AsyncSession) -> None:
    user_id = await seed_user(session)
    wallet = _wallet(session)

    first = await wallet.grant(
        user_id=user_id, amount=50, idempotency_key="cp-txn:p1", reason="cloudpayments_tokens"
    )
    second = await wallet.grant(
        user_id=user_id, amount=50, idempotency_key="cp-txn:p1", reason="cloudpayments_tokens"
    )
    await session.commit()

    assert first.replay is False
    assert second.replay is True
    assert second.tx_id == first.tx_id
    assert await balance_of(session, user_id) == 50  # NOT doubled
    assert len(await ledger_rows(session, user_id)) == 1


async def test_same_key_with_a_different_amount_is_a_409(session: AsyncSession) -> None:
    """Silence here would lose one of the two operations forever, and nobody would ever learn."""
    user_id = await seed_user(session)
    wallet = _wallet(session)
    await wallet.grant(user_id=user_id, amount=50, idempotency_key="k", reason="r")
    with pytest.raises(ConflictError):
        await wallet.grant(user_id=user_id, amount=999, idempotency_key="k", reason="r")


async def test_same_key_with_a_different_type_is_a_409(session: AsyncSession) -> None:
    user_id = await seed_user(session, balance=100)
    wallet = _wallet(session)
    await wallet.grant(user_id=user_id, amount=10, idempotency_key="shared", reason="r")
    with pytest.raises(ConflictError):
        await wallet.consume(user_id=user_id, amount=10, idempotency_key="shared", reason="r")


async def test_repeated_consume_with_the_same_key_debits_once(session: AsyncSession) -> None:
    user_id = await seed_user(session, balance=100)
    wallet = _wallet(session)

    first = await wallet.consume(
        user_id=user_id, amount=40, idempotency_key="generation:g", reason="generation"
    )
    second = await wallet.consume(
        user_id=user_id, amount=40, idempotency_key="generation:g", reason="generation"
    )
    await session.commit()

    assert first.replay is False and second.replay is True
    assert await balance_of(session, user_id) == 60


async def test_consume_beyond_the_balance_is_refused_and_leaves_no_ledger_row(
    session: AsyncSession,
) -> None:
    user_id = await seed_user(session, balance=10)
    wallet = _wallet(session)

    with pytest.raises(InsufficientCreditsError):
        await wallet.consume(
            user_id=user_id, amount=11, idempotency_key="generation:x", reason="generation"
        )
    await session.rollback()

    assert await balance_of(session, user_id) == 10
    assert await ledger_rows(session, user_id) == []  # the insert was rolled back with the raise


@pytest.mark.parametrize("amount", [0, -5])
async def test_non_positive_amounts_are_rejected(session: AsyncSession, amount: int) -> None:
    user_id = await seed_user(session, balance=10)
    wallet = _wallet(session)
    with pytest.raises(ValidationFailedError):
        await wallet.grant(user_id=user_id, amount=amount, idempotency_key="k", reason="r")
    with pytest.raises(ValidationFailedError):
        await wallet.consume(user_id=user_id, amount=amount, idempotency_key="k2", reason="r")


async def test_concurrent_consume_with_one_key_debits_exactly_once(
    session: AsyncSession, sessionmaker_: async_sessionmaker[AsyncSession]
) -> None:
    """Two workers, one idempotency key: the UNIQUE index decides, not the application."""
    user_id = await seed_user(session, balance=100)

    async def _consume() -> str:
        async with sessionmaker_() as s:
            try:
                result = await WalletService(s, AuditService()).consume(
                    user_id=user_id,
                    amount=30,
                    idempotency_key="generation:same",
                    reason="generation",
                )
                await s.commit()
                return "replay" if result.replay else "debit"
            except Exception as exc:  # a serialisation failure is an acceptable loser outcome
                await s.rollback()
                return type(exc).__name__

    outcomes = await asyncio.gather(*[_consume() for _ in range(4)])
    assert outcomes.count("debit") == 1, outcomes
    assert await balance_of(session, user_id) == 70
    assert len(await ledger_rows(session, user_id)) == 1


async def test_concurrent_consume_of_the_whole_balance_never_goes_negative(
    session: AsyncSession, sessionmaker_: async_sessionmaker[AsyncSession]
) -> None:
    """Different keys, one balance: the guard lives in the WHERE of the UPDATE (no read-modify-
    write), so a drained balance simply matches no row."""
    user_id = await seed_user(session, balance=100)

    async def _consume(index: int) -> str:
        async with sessionmaker_() as s:
            try:
                await WalletService(s, AuditService()).consume(
                    user_id=user_id,
                    amount=60,
                    idempotency_key=f"generation:{index}",
                    reason="generation",
                )
                await s.commit()
                return "ok"
            except Exception as exc:
                await s.rollback()
                return type(exc).__name__

    outcomes = await asyncio.gather(*[_consume(i) for i in range(4)])
    assert outcomes.count("ok") == 1
    assert await balance_of(session, user_id) >= 0
    assert await balance_of(session, user_id) == 40


async def test_the_database_refuses_a_negative_balance(session: AsyncSession) -> None:
    user_id = await seed_user(session, balance=5)
    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE wallets SET balance = -1 WHERE user_id = :u"),
            {"u": str(user_id)},
        )
        await session.flush()
    await session.rollback()


async def test_the_database_refuses_a_non_positive_ledger_amount(session: AsyncSession) -> None:
    user_id = await seed_user(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO ledger_transactions (user_id, type, amount, idempotency_key) "
                "VALUES (:u, 'credit', 0, 'k')"
            ),
            {"u": str(user_id)},
        )
        await session.flush()
    await session.rollback()


async def test_get_wallet_does_not_create_a_wallet(session: AsyncSession) -> None:
    user_id = await seed_user(session)
    balance, updated_at = await _wallet(session).get_wallet(user_id)
    assert (balance, updated_at) == (0, None)
    exists = await session.scalar(
        text("SELECT count(*) FROM wallets WHERE user_id = :u"),
        {"u": str(user_id)},
    )
    assert int(exists or 0) == 0
