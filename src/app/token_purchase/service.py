"""Consumable token purchase.

Almost no code of its own — and that is the goal: a new payment scenario must be a COMPOSITION of
the existing pieces (StoreKit verifier + policy state + PaymentsJournal + PRODUCTS), never a new
implementation of money.

Three places where money could leak, and why it does not:

* client sends ``{"credits": 999999}`` → the body has no such field (``extra='forbid'``); the
  amount comes from ``PRODUCTS`` (BR-TP-1);
* client claims an expensive ``productId`` but bought a cheap one → the productId comes from the
  VERIFIED payload (BR-TP-2);
* restore-purchases replays the transaction → ``ledger UNIQUE (user_id, "token-purchase:{txId}")``.

**The subscription guard runs BEFORE the grant** (BR-TP-4): credits without a subscription are
useless (policy blocks anyway), so crediting them would produce the worst possible outcome — money
taken, service not delivered. And note WHERE the guard sits: after cryptographic verification, so
by then Apple HAS taken the money. A ``403`` here is therefore a REFUND-NEEDED event (WARNING +
alert), not a routine validation error — the integrator must check the subscription BEFORE starting
the StoreKit purchase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.outcome import (
    CHANNEL_APPLE,
    OP_TOKEN_PURCHASE,
    RESULT_APPLIED,
    RESULT_REJECTED,
    as_reason,
    emit_billing_outcome,
)
from app.billing.payments import KIND_TOKENS, PaymentsJournal, apple_grant_keys
from app.errors import (
    ProductNotInChannelError,
    SubscriptionRequiredError,
    UnknownProductError,
    ValidationFailedError,
)
from app.policy.engine import SubscriptionStatus
from app.policy.loader import load_policy_state
from app.products import get_products
from app.subscription.storekit import StoreKitVerifier, VerifiedTransaction

_OUTCOME_EVENT = "token_purchase_outcome"


@dataclass(frozen=True)
class PurchaseResult:
    credits_added: int
    new_balance: int
    product_id: str
    transaction_id: str
    idempotent_replay: bool


class TokenPurchaseService:
    def __init__(
        self,
        session: AsyncSession,
        verifier: StoreKitVerifier,
        journal: PaymentsJournal,
    ) -> None:
        self._session = session
        self._verifier = verifier
        self._journal = journal

    async def purchase(self, user_id: uuid.UUID, signed_transaction: str) -> PurchaseResult:
        try:
            tx: VerifiedTransaction = self._verifier.verify(signed_transaction)
        except ValidationFailedError as exc:
            self._emit(RESULT_REJECTED, exc.code, user_id=user_id)
            raise

        product = get_products().get(tx.product_id)
        if product is None or product.kind != KIND_TOKENS:
            self._emit(
                RESULT_REJECTED, "unknown_product", user_id=user_id, product_id=tx.product_id
            )
            raise UnknownProductError("unknown token product")
        if CHANNEL_APPLE not in product.channels:
            self._emit(
                RESULT_REJECTED,
                "product_not_in_channel",
                user_id=user_id,
                product_id=tx.product_id,
            )
            raise ProductNotInChannelError("product is not sold through the App Store")

        if tx.revoked:
            # Refunded by Apple: take the pack's credits back (once), grant nothing.
            refund = await self._journal.record_refund(
                session=self._session,
                channel=CHANNEL_APPLE,
                external_id=f"revocation:{tx.transaction_id}",
                user_id=user_id,
                product_id=tx.product_id,
                event_type="storekit_revocation",
                refunded_grant_keys=apple_grant_keys(tx.transaction_id),
                revoke_subscription=False,
                payload={"transactionId": tx.transaction_id, "productId": tx.product_id},
            )
            self._emit(RESULT_APPLIED, "refunded", user_id=user_id, product_id=tx.product_id)
            balance = await self._session.scalar(
                text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(user_id)}
            )
            return PurchaseResult(
                credits_added=0,
                new_balance=int(balance) if balance is not None else 0,
                product_id=tx.product_id,
                transaction_id=tx.transaction_id,
                idempotent_replay=refund.status == "replayed",
            )

        # GUARD — before the grant, after the (already paid) verification. See module docstring.
        state = await load_policy_state(self._session, user_id)
        if state.subscription_status is not SubscriptionStatus.active:
            self._emit(
                RESULT_REJECTED,
                "subscription_required",
                user_id=user_id,
                product_id=tx.product_id,
            )
            raise SubscriptionRequiredError("an active subscription is required to buy tokens")

        outcome = await self._journal.record_and_grant(
            session=self._session,
            channel=CHANNEL_APPLE,
            external_id=tx.transaction_id,
            user_id=user_id,
            product_id=tx.product_id,
            kind=KIND_TOKENS,
            event_type="storekit_consumable",
            # The prefix is mandatory: a consumable and a subscription can share an Apple id, and
            # without the namespace the second one would silently credit nothing.
            grant_idempotency_key=f"token-purchase:{tx.transaction_id}",
            payload={
                "transactionId": tx.transaction_id,
                "productId": tx.product_id,
                "originalTransactionId": tx.original_transaction_id,
                "environment": tx.environment,
            },
        )
        self._emit(
            RESULT_APPLIED,
            outcome.status,  # granted | replayed
            user_id=user_id,
            product_id=tx.product_id,
            credits=outcome.credits,
        )

        balance = await self._session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(user_id)}
        )
        return PurchaseResult(
            credits_added=outcome.credits,
            new_balance=int(balance) if balance is not None else 0,
            product_id=tx.product_id,
            transaction_id=tx.transaction_id,
            idempotent_replay=outcome.status == "replayed",
        )

    @staticmethod
    def _emit(
        result: str,
        reason: str | None,
        *,
        user_id: uuid.UUID,
        product_id: str | None = None,
        credits: int | None = None,
    ) -> None:
        """One outcome per exit path. Every refusal here happens AFTER Apple took the money."""
        emit_billing_outcome(
            event=_OUTCOME_EVENT,
            channel=CHANNEL_APPLE,
            op=OP_TOKEN_PURCHASE,
            result=result,
            reason=as_reason(reason),
            userId=str(user_id),
            productId=product_id,
            creditsGranted=credits,
        )
