"""Wallet / Ledger — the ONLY writer of ``wallets`` and ``ledger_transactions``.

Three protections act SIMULTANEOUSLY, and none replaces the others:

| mechanism                                   | guarantees            | lives in |
|---------------------------------------------|-----------------------|----------|
| ``UNIQUE (user_id, idempotency_key)``        | idempotency           | DB schema |
| ``UPDATE ... WHERE balance >= amount``       | no race (no RMW)      | SQL |
| ``CHECK (balance >= 0)``                     | non-negative balance  | DB schema |

Redis, caches and in-code checks take no part: money is protected by the database, which is the
only component that sees ALL concurrent transactions. The application (several replicas, several
workers) does not and cannot.

Wallet is an EXECUTOR, not a decider: it does not know about subscriptions (Policy), prices
(PricingPolicy) or where the money came from (billing channels).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_BILLING_CREDIT, EVENT_BILLING_DEBIT, AuditService
from app.errors import ConflictError, InsufficientCreditsError, ValidationFailedError
from app.models import LedgerTransaction
from app.observability.metrics import wallet_debit_total


@dataclass(frozen=True)
class ConsumeResult:
    replay: bool  # True → the operation already happened; no money moved
    tx_id: uuid.UUID
    balance: int  # balance AFTER the operation


@dataclass(frozen=True)
class GrantResult:
    replay: bool
    tx_id: uuid.UUID
    balance: int


class WalletService:
    """Atomic, idempotent ``consume`` / ``grant``.

    Uses the CALLER's session, so the ledger row, the balance update and the audit record all
    commit (or roll back) together — "debited but not recorded" is not a reachable state.
    """

    def __init__(self, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    async def _ensure_wallet(self, user_id: uuid.UUID) -> None:
        """Lazy, idempotent wallet provisioning. The ``users`` row is guaranteed."""
        await self._session.execute(
            text(
                "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0) "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"uid": str(user_id)},
        )

    async def _existing_tx(
        self, user_id: uuid.UUID, idempotency_key: str
    ) -> LedgerTransaction | None:
        row: LedgerTransaction | None = await self._session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.user_id == user_id,
                LedgerTransaction.idempotency_key == idempotency_key,
            )
        )
        return row

    async def _balance(self, user_id: uuid.UUID) -> int:
        """Read the balance with a RAW SELECT — never through the ORM identity map.

        Balances are mutated by raw ``text()`` UPDATEs, which SQLAlchemy does not propagate to an
        already-loaded ``Wallet`` object: an ORM read in the same session could hand back the
        PRE-debit balance. That value would then flow into a policy check or an API response as if
        it were the truth. A raw SELECT always sees what the transaction actually wrote.
        """
        balance = await self._session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(user_id)}
        )
        return int(balance) if balance is not None else 0

    async def _replay_or_conflict(
        self,
        *,
        user_id: uuid.UUID,
        idempotency_key: str,
        expected_type: str,
        amount: int,
    ) -> tuple[uuid.UUID, int]:
        """The key already exists: either a legitimate replay, or a caller bug.

        Same key + same payload → replay (return the ORIGINAL tx, move no money, write NO new
        audit record — nothing happened). Same key + a DIFFERENT amount/type → ``409``: two
        different operations were given one identity; staying silent here would mean one of them
        is lost forever, and nobody would ever learn about it (BR-WAL-3).
        """
        existing = await self._existing_tx(user_id, idempotency_key)
        if existing is None:  # pragma: no cover - the unique index guarantees it exists
            raise ConflictError("idempotency conflict")
        if existing.type != expected_type or int(existing.amount) != amount:
            raise ConflictError("idempotency key reused with a different payload")
        return existing.id, await self._balance(user_id)

    async def consume(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        reason: str,
        meta: dict[str, Any] | None = None,
        generation_id: uuid.UUID | None = None,
    ) -> ConsumeResult:
        """Atomic idempotent DEBIT.

        The balance check lives in the ``WHERE`` of the UPDATE itself — NOT in a preceding
        ``SELECT``. If a concurrent transaction drained the balance first, the UPDATE simply
        matches no row, returns ``None``, and the WHOLE transaction (including the ledger insert)
        is rolled back by the raised error. No ``SELECT FOR UPDATE``, no explicit locks, no race.

        There is deliberately NO public ``POST /v1/wallet/consume``: a debit is a CONSEQUENCE of a
        generation, not an action a client may request.
        """
        if amount <= 0:
            raise ValidationFailedError("amount must be positive")
        await self._ensure_wallet(user_id)

        # Idempotency source of truth: ux_ledger_idempotency (user_id, idempotency_key).
        # ON CONFLICT DO NOTHING (instead of catching UniqueViolation) means a violation of ANY
        # OTHER constraint — an FK, say — still raises and is never mistaken for a replay.
        inserted_id = await self._session.scalar(
            text(
                "INSERT INTO ledger_transactions "
                "(user_id, type, amount, reason, meta, idempotency_key) "
                "VALUES (:uid, 'debit', :amount, :reason, CAST(:meta AS JSONB), :key) "
                "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "uid": str(user_id),
                "amount": amount,
                "reason": reason,
                "meta": json.dumps(meta or {}),
                "key": idempotency_key,
            },
        )
        if inserted_id is None:
            tx_id, balance = await self._replay_or_conflict(
                user_id=user_id,
                idempotency_key=idempotency_key,
                expected_type="debit",
                amount=amount,
            )
            return ConsumeResult(replay=True, tx_id=tx_id, balance=balance)

        updated = await self._session.scalar(
            text(
                "UPDATE wallets SET balance = balance - :amount, updated_at = now() "
                "WHERE user_id = :uid AND balance >= :amount "
                "RETURNING balance"
            ),
            {"uid": str(user_id), "amount": amount},
        )
        if updated is None:
            # Not enough credits → raise, which rolls back the ledger insert above as well.
            # The user can never go negative (and CHECK (balance >= 0) is the last line anyway).
            wallet_debit_total.labels(result="fail").inc()
            raise InsufficientCreditsError("insufficient_credits")

        new_balance = int(updated)
        wallet_debit_total.labels(result="success").inc()
        tx_id = uuid.UUID(str(inserted_id))
        await self._audit.log(
            EVENT_BILLING_DEBIT,
            session=self._session,
            user_id=user_id,
            generation_id=generation_id,
            payload={
                "amount": amount,
                "reason": reason,
                "ledgerTxId": str(tx_id),
                "newBalance": new_balance,
            },
        )
        return ConsumeResult(replay=False, tx_id=tx_id, balance=new_balance)

    async def grant(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        reason: str,
        meta: dict[str, Any] | None = None,
    ) -> GrantResult:
        """Atomic idempotent CREDIT. Symmetric to ``consume``, without the balance guard.

        Callers MUST pass a key that is STABLE for one logical operation and DISTINCT across
        operations — it is bound to a business entity (``generation_id`` / ``transaction_id`` /
        ``payment_id``), never generated per transport retry. Namespaces (``sub-grant:`` /
        ``token-purchase:`` / ``adapty-txn:`` / ``cp-txn:`` / ``admin-grant:`` …) are mandatory:
        without the prefix a consumable and a subscription sharing one Apple transaction id would
        collapse into a single grant.
        """
        if amount <= 0:
            raise ValidationFailedError("amount must be positive")
        await self._ensure_wallet(user_id)

        inserted_id = await self._session.scalar(
            text(
                "INSERT INTO ledger_transactions "
                "(user_id, type, amount, reason, meta, idempotency_key) "
                "VALUES (:uid, 'credit', :amount, :reason, CAST(:meta AS JSONB), :key) "
                "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "uid": str(user_id),
                "amount": amount,
                "reason": reason,
                "meta": json.dumps(meta or {}),
                "key": idempotency_key,
            },
        )
        if inserted_id is None:
            tx_id, balance = await self._replay_or_conflict(
                user_id=user_id,
                idempotency_key=idempotency_key,
                expected_type="credit",
                amount=amount,
            )
            return GrantResult(replay=True, tx_id=tx_id, balance=balance)

        updated = await self._session.scalar(
            text(
                "UPDATE wallets SET balance = balance + :amount, updated_at = now() "
                "WHERE user_id = :uid RETURNING balance"
            ),
            {"uid": str(user_id), "amount": amount},
        )
        if updated is None:
            # The wallet row vanished between _ensure_wallet() and this UPDATE. Unreachable today —
            # but if it ever happens, the invariant "balance == sum(ledger)" is ALREADY broken, and
            # synthesizing a plausible balance (`else amount`) would quietly paper over it while
            # committing the ledger row. Fail instead: the raise rolls back the ledger insert too,
            # so nothing half-applied survives, and the breakage is loud.
            raise ConflictError("wallet row is missing; grant aborted")

        new_balance = int(updated)
        tx_id = uuid.UUID(str(inserted_id))
        await self._audit.log(
            EVENT_BILLING_CREDIT,
            session=self._session,
            user_id=user_id,
            payload={
                "amount": amount,
                "reason": reason,
                "ledgerTxId": str(tx_id),
                "newBalance": new_balance,
            },
        )
        return GrantResult(replay=False, tx_id=tx_id, balance=new_balance)

    async def get_wallet(self, user_id: uuid.UUID) -> tuple[int, Any]:
        """Balance + ``updated_at`` for ``GET /v1/wallet``. No wallet row → ``(0, None)``.

        Read-only (a balance request must not create a wallet) and a RAW SELECT, for the same
        reason as ``_balance()``: an ORM read in a session that just debited via raw SQL could
        return the stale, pre-debit balance.
        """
        row = (
            await self._session.execute(
                text("SELECT balance, updated_at FROM wallets WHERE user_id = :uid"),
                {"uid": str(user_id)},
            )
        ).first()
        if row is None:
            return 0, None
        return int(row[0]), row[1]
