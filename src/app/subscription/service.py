"""Subscription sync: verify JWS → activate → grant through the shared journal.

Four things the client never controls, and that is the module in one line:

* ``productId`` — from the VERIFIED payload, not the request body (cannot claim one product and buy
  another);
* ``expiresAt`` — from the VERIFIED payload (cannot grant himself a term);
* the credit amount — from the server-side ``PRODUCTS`` map (a price change in App Store Connect
  must never silently change what we credit);
* the addressee — the JWT ``sub``.

The grant goes through ``PaymentsJournal``, so an Apple purchase finally lands in ``payments`` — in
the source StoreKit was not journalled at all, and "what did this user buy in Apple" could only be
answered by scanning ``ledger_transactions.meta``.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.outcome import (
    CHANNEL_APPLE,
    OP_SUBSCRIPTION_SYNC,
    RESULT_APPLIED,
    RESULT_REJECTED,
    as_reason,
    emit_billing_outcome,
)
from app.billing.payments import KIND_SUBSCRIPTION, PaymentsJournal, apple_grant_keys
from app.errors import ProductNotInChannelError, UnknownProductError, ValidationFailedError
from app.policy.loader import effective_subscription_status
from app.products import get_products
from app.subscription.storekit import StoreKitVerifier, VerifiedTransaction

_OUTCOME_EVENT = "subscription_sync_outcome"


@dataclass(frozen=True)
class SyncResult:
    status: str
    plan: str | None
    expires_at: datetime.datetime | None
    credits_granted: int
    new_balance: int
    idempotent_replay: bool


@dataclass(frozen=True)
class SubscriptionView:
    status: str
    plan: str | None
    expires_at: datetime.datetime | None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class SubscriptionService:
    def __init__(
        self,
        session: AsyncSession,
        verifier: StoreKitVerifier,
        journal: PaymentsJournal,
    ) -> None:
        self._session = session
        self._verifier = verifier
        self._journal = journal
        # No audit call here: the purchase is audited by PaymentsJournal as `payment_event`
        # (the event catalogue has no `subscription_change` type).

    async def sync(self, user_id: uuid.UUID, signed_transaction: str) -> SyncResult:
        """Verify the StoreKit transaction, activate the subscription and grant the period credits.

        Exactly one outcome (log + metric) is emitted on EVERY exit path — the refusals below all
        happen AFTER Apple already took the money, so they are lost-payment events, not routine
        validation errors, and the operator must see them.
        """
        try:
            tx: VerifiedTransaction = self._verifier.verify(signed_transaction)
        except ValidationFailedError as exc:
            emit_billing_outcome(
                event=_OUTCOME_EVENT,
                channel=CHANNEL_APPLE,
                op=OP_SUBSCRIPTION_SYNC,
                result=RESULT_REJECTED,
                reason=as_reason(exc.code),  # verification_unavailable | invalid_transaction
                userId=str(user_id),
            )
            raise

        product = get_products().get(tx.product_id)
        if product is None or product.kind != KIND_SUBSCRIPTION:
            emit_billing_outcome(
                event=_OUTCOME_EVENT,
                channel=CHANNEL_APPLE,
                op=OP_SUBSCRIPTION_SYNC,
                result=RESULT_REJECTED,
                reason="unknown_product",
                userId=str(user_id),
                productId=tx.product_id,
            )
            raise UnknownProductError("unknown subscription product")
        if CHANNEL_APPLE not in product.channels:
            emit_billing_outcome(
                event=_OUTCOME_EVENT,
                channel=CHANNEL_APPLE,
                op=OP_SUBSCRIPTION_SYNC,
                result=RESULT_REJECTED,
                reason="product_not_in_channel",
                userId=str(user_id),
                productId=tx.product_id,
            )
            raise ProductNotInChannelError("product is not sold through the App Store")

        if tx.revoked:
            return await self._revoked(user_id, tx)

        active = tx.expires_at is not None and tx.expires_at > _now()
        status = "active" if active else "expired"
        await self._upsert_subscription(user_id, status, tx.product_id, tx.expires_at)

        # ONE transaction with the journal + grant (the caller commits). `sub-grant:` is the
        # namespace: without the prefix a consumable and a subscription sharing an Apple id would
        # collapse into one ledger key and the second would silently credit nothing.
        outcome = await self._journal.record_and_grant(
            session=self._session,
            channel=CHANNEL_APPLE,
            external_id=tx.transaction_id,
            user_id=user_id,
            product_id=tx.product_id,
            kind=KIND_SUBSCRIPTION,
            event_type="storekit_subscription",
            grant_idempotency_key=f"sub-grant:{tx.transaction_id}",
            payload={
                "transactionId": tx.transaction_id,
                "productId": tx.product_id,
                "originalTransactionId": tx.original_transaction_id,
                "environment": tx.environment,
                "expiresDate": tx.expires_at.isoformat() if tx.expires_at else None,
            },
        )

        emit_billing_outcome(
            event=_OUTCOME_EVENT,
            channel=CHANNEL_APPLE,
            op=OP_SUBSCRIPTION_SYNC,
            result=RESULT_APPLIED,
            reason=as_reason(outcome.status),  # granted | replayed
            userId=str(user_id),
            productId=tx.product_id,
            creditsGranted=outcome.credits,
        )

        balance, _ = await self._balance(user_id)
        return SyncResult(
            status=status,
            plan=tx.product_id,
            expires_at=tx.expires_at,
            credits_granted=outcome.credits,
            new_balance=balance,
            idempotent_replay=outcome.status == "replayed",
        )

    async def _revoked(self, user_id: uuid.UUID, tx: VerifiedTransaction) -> SyncResult:
        """Apple revoked the transaction (refund / family-sharing removal): access ends and
        the period's credits are taken back — whichever channel granted them. Granting on a
        revoked transaction (what this path used to do on a first sync) would pay out a refund."""
        outcome = await self._journal.record_refund(
            session=self._session,
            channel=CHANNEL_APPLE,
            external_id=f"revocation:{tx.transaction_id}",
            user_id=user_id,
            product_id=tx.product_id,
            event_type="storekit_revocation",
            refunded_grant_keys=apple_grant_keys(tx.transaction_id),
            revoke_subscription=True,
            payload={"transactionId": tx.transaction_id, "productId": tx.product_id},
        )
        emit_billing_outcome(
            event=_OUTCOME_EVENT,
            channel=CHANNEL_APPLE,
            op=OP_SUBSCRIPTION_SYNC,
            result=RESULT_APPLIED,
            reason="refunded",
            userId=str(user_id),
            productId=tx.product_id,
        )
        balance, _ = await self._balance(user_id)
        return SyncResult(
            status="expired",
            plan=tx.product_id,
            expires_at=tx.expires_at,
            credits_granted=0,
            new_balance=balance,
            idempotent_replay=outcome.status == "replayed",
        )

    async def get_subscription(self, user_id: uuid.UUID) -> SubscriptionView:
        """Status AFTER lazy expiry — i.e. exactly what the Policy Engine will see.

        Reading the stored column directly would report ``active`` for a subscription that expired
        an hour ago (there is no background expiry job) — the API would then disagree with the
        policy, which is the classic trust bug.
        """
        row = (
            await self._session.execute(
                text("SELECT status, plan, expires_at FROM subscriptions WHERE user_id = :uid"),
                {"uid": str(user_id)},
            )
        ).first()
        if row is None:
            return SubscriptionView(status="none", plan=None, expires_at=None)
        effective = effective_subscription_status(row[0], row[2])
        return SubscriptionView(status=effective.value, plan=row[1], expires_at=row[2])

    async def _upsert_subscription(
        self,
        user_id: uuid.UUID,
        status: str,
        plan: str,
        expires_at: datetime.datetime | None,
    ) -> None:
        await self._session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at) "
                "VALUES (:uid, CAST(:status AS subscription_status), :plan, :expires_at, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "status = EXCLUDED.status, plan = EXCLUDED.plan, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()"
            ),
            {
                "uid": str(user_id),
                "status": status,
                "plan": plan,
                "expires_at": expires_at,
            },
        )

    async def _balance(self, user_id: uuid.UUID) -> tuple[int, None]:
        balance = await self._session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(user_id)}
        )
        return int(balance) if balance is not None else 0, None
