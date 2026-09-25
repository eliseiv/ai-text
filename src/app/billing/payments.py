"""``PaymentsJournal`` — the ONE money path of all three payment channels.

A channel parses and verifies (formats and trust anchors genuinely differ). **Everything about
money is here** — delivery dedup, anti-tamper, the grant, the journal row. In the source each
channel had its own journal, its own product map and its own user resolution; the duplication is
what let a critical fix stay un-migrated and fire the same prod incident twice.

TWO LAYERS OF IDEMPOTENCY — THE central invariant of this module
---------------------------------------------------------------
They are DIFFERENT COLUMNS, and merging them silently doubles money:

    LAYER 1 (delivery)  payments UNIQUE (channel, external_id)     — the SAME webhook again
    LAYER 2 (grant)     ledger   UNIQUE (user_id, idempotency_key) — DIFFERENT events, one period

One purchase makes Adapty send two events: ``trial_started`` (profile_event_id=E1) and
``subscription_started`` (E2) — different event ids, **one** ``transaction_id`` T1. Key the grant
on the event id and the balance doubles, silently. Key the delivery dedup on the transaction id
and the second event's arrival is lost. Right answer: 2 rows in ``payments``, 1 row in ``ledger``.

TRANSACTION BOUNDARY — the OPPOSITE of the generation anchor
-----------------------------------------------------------
``record_and_grant()`` is ATOMIC: layer 1 and layer 2 commit together. Committing layer 1 on its
own is **forbidden** — and this is exactly the reverse of the identical-looking
``INSERT ... ON CONFLICT DO NOTHING RETURNING`` in the generation anchor, where a COMMIT before the
provider call is **mandatory**. The rule is not in the SQL pattern, it is in what comes NEXT:
a network call lasting minutes → commit (never hold a pooled connection); a local ``wallet.grant()``
in the same DB → do not commit (splitting the transaction loses the payment):

    INSERT payments → COMMIT      ← row exists
      ✗ crash
    wallet.grant(...)             ← never ran
    aggregator retries the same webhook:
    INSERT ... ON CONFLICT DO NOTHING → nothing returned → "replayed", 0 credits
    ⇒ the grant will NEVER happen. Silently lost payment.

Hence the INSERT starts at the NEUTRAL status ``received`` (not an optimistic ``granted``): a row
that somehow got committed without a grant is honestly visible as unfinished. The diagnostic query
``SELECT * FROM payments WHERE status='received'`` must always return nothing.
"""

from __future__ import annotations

import decimal
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_PAYMENT_EVENT, EVENT_PAYMENT_REFUND, AuditService
from app.observability.metrics import payment_events_total
from app.products import get_products
from app.wallet.service import WalletService

logger = logging.getLogger("app.billing.payments")

PaymentStatus = Literal["granted", "replayed", "no_grant", "rejected", "refunded"]
PaymentLayer = Literal["delivery", "grant"]

KIND_SUBSCRIPTION = "subscription"
KIND_TOKENS = "tokens"
KIND_SUBSCRIPTION_EVENT = "subscription_event"
KIND_REFUND = "refund"

REASON_DUPLICATE_DELIVERY = "duplicate_delivery"
REASON_UNKNOWN_PRODUCT = "unknown_product"
REASON_PRODUCT_NOT_IN_CHANNEL = "product_not_in_channel"


@dataclass(frozen=True)
class RefundOutcome:
    """What a refund did. ``credits_not_recovered`` > 0: already spent, the balance hit zero."""

    status: Literal["refunded", "replayed"]
    credits_revoked: int = 0
    credits_not_recovered: int = 0
    grants_found: int = 0
    subscription_revoked: bool = False
    payment_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PaymentOutcome:
    """What the journal did. ``layer`` is REQUIRED whenever ``status == "replayed"``.

    The two kinds of replay are different phenomena and must stay distinguishable on the
    dashboard: ``delivery`` is the same webhook arriving twice (layer 1), ``grant`` is another
    event of the same billing period (layer 2). If layer 2 ever regresses, money doubles — and
    without this label nobody would see it happen.
    """

    status: PaymentStatus
    credits: int
    layer: PaymentLayer | None = None
    ledger_tx_id: uuid.UUID | None = None
    payment_id: uuid.UUID | None = None
    reason: str | None = None


# Per-channel payload allowlist. What is stored is a PROJECTION, never the raw body:
# a CloudPayments callback carries fragments of the card number, and a raw dump would persist them
# forever. Keys not listed here are dropped.
_PAYLOAD_ALLOWLIST: dict[str, frozenset[str]] = {
    "apple_storekit": frozenset(
        {"transactionId", "productId", "originalTransactionId", "environment", "expiresDate"}
    ),
    "adapty": frozenset(
        {
            "profileEventId",
            "eventType",
            "transactionId",
            "vendorProductId",
            "isActive",
            "expiresAt",
        }
    ),
    "cloudpayments": frozenset({"paymentId", "productCode", "paymentType", "status", "paidAt"}),
}
# Added to every channel's allowlist for refund rows (our own numbers, no provider data).
_REFUND_PAYLOAD_KEYS = frozenset(
    {"refundOf", "creditsRevoked", "creditsNotRecovered", "subscriptionRevoked"}
)


def sanitize_payload(channel: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Project ``payload`` onto the channel allowlist. Anything unlisted never reaches the DB."""
    allowed = _PAYLOAD_ALLOWLIST.get(channel, frozenset()) | _REFUND_PAYLOAD_KEYS
    return {k: v for k, v in payload.items() if k in allowed and v is not None}


class PaymentsJournal:
    """The ONLY writer of ``payments`` and the only initiator of payment grants."""

    def __init__(self, wallet: WalletService, audit: AuditService) -> None:
        self._wallet = wallet
        self._audit = audit

    async def record_and_grant(
        self,
        *,
        session: AsyncSession,
        channel: str,
        external_id: str,
        user_id: uuid.UUID,
        product_id: str | None,
        kind: str,
        event_type: str | None = None,
        grant_idempotency_key: str | None,
        amount: decimal.Decimal | None = None,
        currency: str | None = None,
        payload: dict[str, Any] | None = None,
        resolved_via: str | None = None,
    ) -> PaymentOutcome:
        """Journal one payment event and grant its credits — atomically.

        ⚠ NO ``session.commit()`` ANYWHERE IN THIS METHOD, and callers must not commit between the
        layers either. The whole body runs inside the caller's transaction; the caller commits once
        afterwards. See the module docstring for the cost of splitting it.
        """
        # ═══ LAYER 1 — DELIVERY dedup. Neutral start status, zero credits. ═══
        payment_id = await session.scalar(
            text(
                "INSERT INTO payments "
                "(user_id, channel, external_id, event_type, product_id, kind, status, "
                " credits_granted, amount, currency, grant_idempotency_key, payload) "
                "VALUES (:uid, CAST(:channel AS payment_channel), :external_id, :event_type, "
                " :product_id, CAST(:kind AS payment_kind), 'received', 0, :amount, :currency, "
                " :grant_key, CAST(:payload AS JSONB)) "
                "ON CONFLICT (channel, external_id) DO NOTHING "
                "RETURNING id"
            ),
            {
                "uid": str(user_id),
                "channel": channel,
                "external_id": external_id,
                "event_type": event_type,
                "product_id": product_id,
                "kind": kind,
                "amount": amount,  # INFORMATIONAL — never the source of credits (BR-8)
                "currency": currency,
                "grant_key": grant_idempotency_key,
                "payload": _json(sanitize_payload(channel, payload or {})),
            },
        )
        if payment_id is None:
            # The very same webhook/transaction arrived again. No mutations at all.
            return self._observed(
                PaymentOutcome(
                    status="replayed",
                    credits=0,
                    layer="delivery",
                    reason=REASON_DUPLICATE_DELIVERY,
                ),
                channel=channel,
                kind=kind,
            )
        payment_uuid = uuid.UUID(str(payment_id))

        # ═══ ANTI-TAMPER (BR-8) — fail-closed. There is NO fallback grant, anywhere. ═══
        # Crediting a "default" amount for a product nobody configured means crediting a number
        # nobody chose — the same BR-8 violation as taking the amount from the callback body, just
        # from the other side. A missing product is a MIS-CONFIGURATION and must be loud.
        product = get_products().get(product_id) if product_id else None
        if kind != KIND_SUBSCRIPTION_EVENT:
            if product is None:
                await self._mark(session, payment_uuid, status="rejected")
                return self._observed(
                    PaymentOutcome(
                        status="rejected",
                        credits=0,
                        payment_id=payment_uuid,
                        reason=REASON_UNKNOWN_PRODUCT,
                    ),
                    channel=channel,
                    kind=kind,
                )
            if channel not in product.channels:
                # The hole the source had: a CloudPayments callback naming an Apple productId would
                # have been credited at the Apple tier. Now it gets nothing.
                await self._mark(session, payment_uuid, status="rejected")
                return self._observed(
                    PaymentOutcome(
                        status="rejected",
                        credits=0,
                        payment_id=payment_uuid,
                        reason=REASON_PRODUCT_NOT_IN_CHANNEL,
                    ),
                    channel=channel,
                    kind=kind,
                )

        if grant_idempotency_key is None or product is None:
            # A valid event that grants nothing by its nature (expiry / cancellation / auto-renew
            # switched off). Journalled, zero credits.
            await self._mark(session, payment_uuid, status="no_grant")
            return self._observed(
                PaymentOutcome(status="no_grant", credits=0, payment_id=payment_uuid),
                channel=channel,
                kind=kind,
            )

        # ═══ LAYER 2 — GRANT idempotency (a different key, a different column). ═══
        credits = product.credits  # ONLY the server-side map. Never `amount`, never the body.
        grant = await self._wallet.grant(
            user_id=user_id,
            amount=credits,
            idempotency_key=grant_idempotency_key,
            reason=f"{channel}_{kind}",
            meta={"channel": channel, "productId": product_id, "paymentId": str(payment_uuid)},
        )
        status: PaymentStatus = "replayed" if grant.replay else "granted"
        credits_granted = 0 if grant.replay else credits
        await self._finalize(
            session,
            payment_uuid,
            status=status,
            credits_granted=credits_granted,
            ledger_tx_id=grant.tx_id,
        )
        await self._audit.log(
            EVENT_PAYMENT_EVENT,
            session=session,
            user_id=user_id,
            payload={
                "channel": channel,
                "kind": kind,
                "status": status,
                "productId": product_id,
                "creditsGranted": credits_granted,
                "resolvedVia": resolved_via,
                "ledgerTxId": str(grant.tx_id),
            },
        )
        return self._observed(
            PaymentOutcome(
                status=status,
                credits=credits_granted,
                # A second event of the SAME billing period — layer 2, not layer 1.
                layer="grant" if grant.replay else None,
                ledger_tx_id=grant.tx_id,
                payment_id=payment_uuid,
            ),
            channel=channel,
            kind=kind,
        )
        # COMMIT happens in the caller: either BOTH the payments row and the grant, or neither.

    async def record_refund(
        self,
        *,
        session: AsyncSession,
        channel: str,
        external_id: str,
        user_id: uuid.UUID,
        product_id: str | None,
        event_type: str | None,
        refunded_grant_keys: Sequence[str],
        revoke_subscription: bool,
        payload: dict[str, Any] | None = None,
        resolved_via: str | None = None,
    ) -> RefundOutcome:
        """Journal a refund and take back what the refunded purchase granted — atomically.

        The same two-layer idempotency as a grant, mirrored:

        * LAYER 1 — the refund EVENT (``channel, external_id``): the same webhook twice → replay;
        * LAYER 2 — the refunded GRANT: its credit is reversed by a debit keyed
          ``refund:<grant key>``, so a purchase reported refunded by TWO channels (the Adapty
          webhook and a revoked StoreKit transaction) is reversed exactly once.

        ``refunded_grant_keys``: every key the refunded purchase may have been granted under.

        Policy (owner's decision): take the granted credits back, but never below zero — credits
        already spent are not recovered (the balance CHECK forbids debt); the shortfall is recorded
        as ``creditsNotRecovered``. A refunded subscription ends access immediately.

        ⚠ No commit here — the caller commits once, exactly as with ``record_and_grant``.
        """
        payment_id = await session.scalar(
            text(
                "INSERT INTO payments "
                "(user_id, channel, external_id, event_type, product_id, kind, status, "
                " credits_granted, payload) "
                "VALUES (:uid, CAST(:channel AS payment_channel), :external_id, :event_type, "
                " :product_id, 'refund', 'received', 0, CAST(:payload AS JSONB)) "
                "ON CONFLICT (channel, external_id) DO NOTHING RETURNING id"
            ),
            {
                "uid": str(user_id),
                "channel": channel,
                "external_id": external_id,
                "event_type": event_type,
                "product_id": product_id,
                "payload": _json(sanitize_payload(channel, payload or {})),
            },
        )
        if payment_id is None:
            payment_events_total.labels(
                channel=channel, kind=KIND_REFUND, status="replayed", layer="delivery"
            ).inc()
            return RefundOutcome(status="replayed")
        payment_uuid = uuid.UUID(str(payment_id))

        credits = (
            await session.execute(
                text(
                    "SELECT idempotency_key, amount FROM ledger_transactions "
                    "WHERE user_id = :uid AND type = 'credit' "
                    "AND idempotency_key = ANY(CAST(:keys AS text[])) ORDER BY created_at"
                ),
                {"uid": str(user_id), "keys": list(dict.fromkeys(refunded_grant_keys))},
            )
        ).all()
        revoked = 0
        not_recovered = 0
        last_tx: uuid.UUID | None = None
        for grant_key, amount in credits:
            reversal_key = f"refund:{grant_key}"
            already = await session.scalar(
                text(
                    "SELECT 1 FROM ledger_transactions "
                    "WHERE user_id = :uid AND idempotency_key = :key"
                ),
                {"uid": str(user_id), "key": reversal_key},
            )
            if already is not None:
                continue  # reversed earlier by a refund event of another channel
            balance = int(
                await session.scalar(
                    text("SELECT balance FROM wallets WHERE user_id = :uid"),
                    {"uid": str(user_id)},
                )
                or 0
            )
            take = min(int(amount), balance)
            not_recovered += int(amount) - take
            if take > 0:
                debit = await self._wallet.consume(
                    user_id=user_id,
                    amount=take,
                    idempotency_key=reversal_key,
                    reason=f"{channel}_refund",
                    meta={"refundOf": grant_key, "paymentId": str(payment_uuid)},
                )
                revoked += take
                last_tx = debit.tx_id

        if revoke_subscription:
            await session.execute(
                text(
                    "UPDATE subscriptions SET status = 'expired', updated_at = now() "
                    "WHERE user_id = :uid"
                ),
                {"uid": str(user_id)},
            )

        await session.execute(
            text(
                "UPDATE payments SET status = 'refunded', ledger_tx_id = :tx, "
                "processed_at = now(), payload = payload || CAST(:extra AS JSONB) "
                "WHERE id = :pid"
            ),
            {
                "tx": str(last_tx) if last_tx else None,
                "pid": str(payment_uuid),
                "extra": _json(
                    {
                        "refundOf": ",".join(str(c[0]) for c in credits) or None,
                        "creditsRevoked": revoked,
                        "creditsNotRecovered": not_recovered,
                        "subscriptionRevoked": revoke_subscription,
                    }
                ),
            },
        )
        await self._audit.log(
            EVENT_PAYMENT_REFUND,
            session=session,
            user_id=user_id,
            payload={
                "channel": channel,
                "productId": product_id,
                "grantsFound": len(credits),
                "creditsRevoked": revoked,
                "creditsNotRecovered": not_recovered,
                "subscriptionRevoked": revoke_subscription,
                "resolvedVia": resolved_via,
            },
        )
        payment_events_total.labels(
            channel=channel, kind=KIND_REFUND, status="refunded", layer="none"
        ).inc()
        return RefundOutcome(
            status="refunded",
            credits_revoked=revoked,
            credits_not_recovered=not_recovered,
            grants_found=len(credits),
            subscription_revoked=revoke_subscription,
            payment_id=payment_uuid,
        )

    @staticmethod
    async def _mark(session: AsyncSession, payment_id: uuid.UUID, *, status: str) -> None:
        """Finalize a row that grants nothing (rejected / no_grant): credits stay 0."""
        await session.execute(
            text(
                "UPDATE payments SET status = CAST(:status AS payment_status), "
                "processed_at = now() WHERE id = :pid"
            ),
            {"status": status, "pid": str(payment_id)},
        )

    @staticmethod
    async def _finalize(
        session: AsyncSession,
        payment_id: uuid.UUID,
        *,
        status: str,
        credits_granted: int,
        ledger_tx_id: uuid.UUID,
    ) -> None:
        """Finalize a granted/replayed row. ``ledger_tx_id`` is always set → ck_payments_grant_link
        holds, and no credited amount can exist outside the ledger."""
        await session.execute(
            text(
                "UPDATE payments SET status = CAST(:status AS payment_status), "
                "credits_granted = :credits, ledger_tx_id = :tx, processed_at = now() "
                "WHERE id = :pid"
            ),
            {
                "status": status,
                "credits": credits_granted,
                "tx": str(ledger_tx_id),
                "pid": str(payment_id),
            },
        )

    @staticmethod
    def _observed(outcome: PaymentOutcome, *, channel: str, kind: str) -> PaymentOutcome:
        """Emit ``payment_events_total`` — the JOURNAL metric (only when a row exists).

        This is NOT the alerting metric: it is silent on every path that ends before the row is
        created (``user_not_found`` above all). That is what ``billing_outcome_total`` is for.
        """
        payment_events_total.labels(
            channel=channel,
            kind=kind,
            status=outcome.status,
            layer=outcome.layer or "none",
        ).inc()
        return outcome


def apple_grant_keys(transaction_id: str) -> tuple[str, ...]:
    """Every ledger key an Apple transaction can have been granted under.

    ONE Apple purchase reaches us twice — the client's StoreKit sync AND the Adapty webhook — so
    both channels grant under the SAME key (``sub-grant:`` / ``token-purchase:`` + the Apple
    transaction id): layer 2 of the journal then credits it once. ``adapty-txn:`` is the
    template's former Adapty namespace, kept here so a refund still finds such a grant.
    """
    return (
        f"sub-grant:{transaction_id}",
        f"token-purchase:{transaction_id}",
        f"adapty-txn:{transaction_id}",
    )


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value)
