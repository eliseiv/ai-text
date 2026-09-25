"""Admin operations.

**TWO operations exist, and one of them is not obvious.** "Just grant credits" is wrong: access
= subscription (the RIGHT) AND credits (the RESOURCE). With ``subscription='none'`` the balance is
not even consulted — the operator can credit 1000 and the user stays blocked. Restoring access
needs the subscription activated too.

``grant_subscription()`` is a SEPARATE method, not a flag on ``SubscriptionService``. A flag "skip
verification" would create, INSIDE the payment service, a code path that bypasses transaction
verification — and code that exists can be called from somewhere it should not be. This method is
simply not connected to the verify path.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_ADMIN_GRANT, EVENT_ADMIN_SUBSCRIPTION_GRANT, AuditService
from app.config import get_settings
from app.errors import UserNotFoundError
from app.observability.metrics import admin_grant_total
from app.wallet.service import WalletService

GRANT_KIND_CREDITS = "credits"
GRANT_KIND_SUBSCRIPTION = "subscription"


@dataclass(frozen=True)
class AdminGrantResult:
    credits_granted: int
    new_balance: int
    ledger_tx_id: uuid.UUID
    idempotent_replay: bool


@dataclass(frozen=True)
class AdminSubscriptionGrantResult:
    status: str
    expires_at: datetime.datetime
    plan: str
    credits_granted: int
    new_balance: int | None = None
    ledger_tx_id: uuid.UUID | None = None
    idempotent_replay: bool | None = None


@dataclass(frozen=True)
class AdminWalletView:
    user_id: uuid.UUID
    balance: int
    updated_at: datetime.datetime | None
    recent_transactions: list[dict[str, Any]]


class AdminService:
    def __init__(self, session: AsyncSession, wallet: WalletService, audit: AuditService) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit

    async def _require_user_exists(self, user_id: uuid.UUID) -> None:
        """Admin NEVER creates users: an unknown userId is a 404.

        This is a typo guard: crediting a phantom id would look like success while the real user
        still has nothing.
        """
        exists = await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :uid"), {"uid": str(user_id)}
        )
        if exists is None:
            raise UserNotFoundError("user not found")

    async def grant(
        self,
        *,
        user_id: uuid.UUID,
        credits: int,
        idempotency_key: str,
        reason: str | None,
    ) -> AdminGrantResult:
        """Credit an operator grant. Idempotent by the MANDATORY ``idempotencyKey``.

        The key is mandatory because a double-click must not credit twice; the same key with a
        DIFFERENT amount is a 409 from the wallet (a caller bug — staying silent would lose one of
        the two operations forever).
        """
        await self._require_user_exists(user_id)
        result = await self._wallet.grant(
            user_id=user_id,
            amount=credits,
            idempotency_key=f"admin-grant:{idempotency_key}",
            reason="admin_grant",
            meta={"source": "admin", "reason": reason},
        )
        await self._audit.log(
            EVENT_ADMIN_GRANT,
            session=self._session,
            user_id=user_id,
            payload={
                # No secret ever lands here. `idempotencyKey` is the ONLY attribution thread of an
                # admin grant ("who credited, under which ticket") — it must survive redaction.
                "actor": "admin",
                "credits": credits,
                "idempotencyKey": idempotency_key,
                "reason": reason,
                "ledgerTxId": str(result.tx_id),
                "idempotentReplay": result.replay,
            },
        )
        admin_grant_total.labels(kind=GRANT_KIND_CREDITS).inc()
        return AdminGrantResult(
            credits_granted=0 if result.replay else credits,
            new_balance=result.balance,
            ledger_tx_id=result.tx_id,
            idempotent_replay=result.replay,
        )

    async def grant_subscription(
        self,
        *,
        user_id: uuid.UUID,
        expires_at: datetime.datetime,
        plan: str,
        credits: int | None,
        idempotency_key: str,
    ) -> AdminSubscriptionGrantResult:
        """Activate a subscription WITHOUT a StoreKit transaction, and credit the period.

        ``credits`` semantics (the default is load-bearing):
          * omitted → ``SUBSCRIPTION_CREDITS_PER_PERIOD`` — the operator must not have to remember
            the package size, and the default MUST yield WORKING access. A default of 0 would hand
            out a subscription with an empty wallet → policy returns ``credits_empty`` → the user is
            blocked again, and the operator thinks he fixed it;
          * ``0`` explicitly → activate without crediting (the user already has credits);
          * ``N`` → credit exactly N.

        Subscription upsert and grant run in ONE transaction: a failure on the grant leaves no
        half-activated subscription behind.
        """
        await self._require_user_exists(user_id)
        effective = (
            credits if credits is not None else get_settings().subscription_credits_per_period
        )

        await self._session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at) "
                "VALUES (:uid, 'active', :plan, :expires_at, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET status = 'active', plan = EXCLUDED.plan, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()"
            ),
            {"uid": str(user_id), "plan": plan, "expires_at": expires_at},
        )

        new_balance: int | None = None
        ledger_tx_id: uuid.UUID | None = None
        idempotent_replay: bool | None = None
        granted = 0
        if effective > 0:
            grant = await self._wallet.grant(
                user_id=user_id,
                amount=effective,
                # A different namespace from admin-grant: the same idempotencyKey used for both
                # operations must not collapse them into one.
                idempotency_key=f"admin-sub-grant:{idempotency_key}",
                reason="admin_subscription_grant",
                meta={"source": "admin", "plan": plan},
            )
            new_balance = grant.balance
            ledger_tx_id = grant.tx_id
            idempotent_replay = grant.replay
            granted = 0 if grant.replay else effective

        payload: dict[str, Any] = {
            "actor": "admin",
            "plan": plan,
            "status": "active",
            "expiresAt": expires_at.isoformat(),
            "credits": granted,
            "idempotencyKey": idempotency_key,
        }
        if ledger_tx_id is not None:
            payload["ledgerTxId"] = str(ledger_tx_id)
        await self._audit.log(
            EVENT_ADMIN_SUBSCRIPTION_GRANT,
            session=self._session,
            user_id=user_id,
            payload=payload,
        )
        admin_grant_total.labels(kind=GRANT_KIND_SUBSCRIPTION).inc()
        return AdminSubscriptionGrantResult(
            status="active",
            expires_at=expires_at,
            plan=plan,
            credits_granted=granted,
            new_balance=new_balance,
            ledger_tx_id=ledger_tx_id,
            idempotent_replay=idempotent_replay,
        )

    async def get_wallet_view(self, user_id: uuid.UUID, last_n: int = 20) -> AdminWalletView:
        """Read-only support view. Unknown userId → 404 (never creates the user)."""
        await self._require_user_exists(user_id)
        balance, updated_at = await self._wallet.get_wallet(user_id)
        rows = (
            await self._session.execute(
                text(
                    "SELECT type, amount, reason, created_at FROM ledger_transactions "
                    "WHERE user_id = :uid ORDER BY created_at DESC LIMIT :n"
                ),
                {"uid": str(user_id), "n": last_n},
            )
        ).all()
        return AdminWalletView(
            user_id=user_id,
            balance=balance,
            updated_at=updated_at,
            recent_transactions=[
                {
                    "type": r[0],
                    "amount": int(r[1]),
                    "reason": r[2],
                    "createdAt": r[3].isoformat() if r[3] else None,
                }
                for r in rows
            ],
        )
