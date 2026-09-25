"""The public RU webhook: callback = TRIGGER, verify = truth.

Order matters, and each step is a defence:

0. **config gate** — no ``CLOUDPAYMENTS_API_TOKEN`` → 500 (we cannot verify ⇒ we must not credit;
   the aggregator retries until the operator configures the channel);
1. **body shape** → ``ignored`` (no DB, no outgoing call);
2. **gate** ``Status=Completed`` + ``OperationType=Payment``;
3. **resolve** the user via the SHARED resolver → miss = ``user_not_found``, WARNING,
   **and NO outgoing verify** — a garbage callback must not make us call the aggregator
   (anti-amplification: the endpoint is public);
4. **verify** through the aggregator API with OUR key — the ONLY trigger of a grant. Transient
   failure → **500 retriable** (never 200: 200 means "accepted" and the payment would be lost);
5. **reconcile** — confirmed status, inside the freshness window;
6. **credit** each payment through ``PaymentsJournal``, keyed by the ``payment_id`` FROM VERIFY.

Why the callback's own ``TransactionId`` is never a key: we do not trust the callback, so its
identifier cannot key money — an attacker "occupying" a TransactionId would otherwise block a real
grant.
"""

from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.outcome import (
    CHANNEL_CLOUDPAYMENTS,
    OP_WEBHOOK,
    RESULT_APPLIED,
    RESULT_DUPLICATE,
    RESULT_ERROR,
    RESULT_IGNORED,
    RESULT_REJECTED,
    as_reason,
    emit_billing_outcome,
)
from app.billing.payments import PaymentsJournal
from app.billing_cloudpayments import parser
from app.billing_cloudpayments.verify import (
    CloudPaymentsVerifyClient,
    CreditablePayment,
    RefundedPayment,
    payment_statuses,
    select_creditable_payments,
    select_refunded_payments,
)
from app.billing_common.resolve import resolve_user
from app.config import CoreSettings
from app.errors import (
    CloudPaymentsVerificationUnavailableError,
    CloudPaymentsWebhookMisconfiguredError,
)
from app.products import add_period, get_products

_OUTCOME_EVENT = "cloudpayments_webhook_outcome"
_SUBSCRIPTION_DEFAULT_DAYS = 30


@dataclass(frozen=True)
class WebhookOutcome:
    result: str
    reason: str | None = None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class CloudPaymentsWebhookService:
    def __init__(
        self,
        session: AsyncSession,
        journal: PaymentsJournal,
        settings: CoreSettings,
        verify_client: CloudPaymentsVerifyClient,
    ) -> None:
        self._session = session
        self._journal = journal
        self._settings = settings
        self._verify = verify_client

    def _emit(
        self,
        outcome: WebhookOutcome,
        *,
        transaction_id: str | None = None,
        user_id: uuid.UUID | None = None,
        resolved_via: str | None = None,
        verify_result: str | None = None,
        credited_count: int | None = None,
        statuses: list[str] | None = None,
        product_id: str | None = None,
    ) -> WebhookOutcome:
        """EXACTLY ONE outcome per callback. Allowlist — card PII, `Data`, bearer, amount/currency
        and customerEmail never appear here (they are not even parsed)."""
        emit_billing_outcome(
            event=_OUTCOME_EVENT,
            channel=CHANNEL_CLOUDPAYMENTS,
            op=OP_WEBHOOK,
            result=outcome.result,
            reason=as_reason(outcome.reason),
            transactionId=transaction_id,
            userId=str(user_id) if user_id else None,
            resolvedVia=resolved_via,
            verify=verify_result,
            creditedCount=credited_count,
            paymentStatuses=statuses,
            productId=product_id,
        )
        return outcome

    async def handle(self, raw: bytes) -> WebhookOutcome:
        # --- 0. channel activation gate: without the API token we cannot verify ⇒ must not credit.
        # This path emits an outcome too: an unconfigured channel while the aggregator is sending
        # callbacks means real payments are piling up unverified — an operator must see it.
        if not self._settings.cloudpayments_api_token:
            self._emit(WebhookOutcome(RESULT_ERROR, "not_configured"))
            raise CloudPaymentsWebhookMisconfiguredError("cloudpayments api token not configured")

        # --- 1. body shape (no DB, no outgoing call)
        if not raw:
            return self._emit(WebhookOutcome(RESULT_IGNORED, "empty_body"))
        try:
            body: Any = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._emit(WebhookOutcome(RESULT_IGNORED, "invalid_json"))
        if not isinstance(body, dict):
            return self._emit(WebhookOutcome(RESULT_IGNORED, "not_an_object"))

        # --- 2. gate
        transaction_id = parser.parse_transaction_id(body)  # log context only
        if not parser.parse_gate(parser.parse_status(body), parser.parse_operation_type(body)):
            return self._emit(
                WebhookOutcome(RESULT_IGNORED, "not_a_completed_payment"),
                transaction_id=transaction_id,
            )

        data = parser.parse_data(body)
        device_id = parser.parse_device_id(body, data)
        if device_id is None or not parser.is_uuid(device_id):
            # Anti-SSRF: only a canonical UUID may ever be interpolated into the verify path.
            return self._emit(
                WebhookOutcome(RESULT_IGNORED, "invalid_account_id"), transaction_id=transaction_id
            )

        # --- 3. resolve (shared resolver; never provisions) — BEFORE any outgoing call
        resolved = await resolve_user(self._session, device_id)
        if resolved is None:
            return self._emit(
                WebhookOutcome(RESULT_IGNORED, "user_not_found"), transaction_id=transaction_id
            )
        user_id, resolved_via = resolved

        # Close the read transaction BEFORE the network call: a 15 s outgoing request must never
        # hold a pooled DB connection open.
        await self._session.commit()

        # --- 4. verify (the trust anchor). A transient failure raises → 500 retriable.
        # The outcome MUST be emitted on this path too: without it the declared exit path
        # (verify_failed → upstream) would be unreachable, and the alert built on it would be a
        # dead alert — the exact defect the mandatory-outcome rule exists to remove ("exactly one
        # outcome per call, on EVERY exit path").
        try:
            payments_data = await self._verify.list_payments(device_id=device_id)
        except CloudPaymentsVerificationUnavailableError:
            self._emit(
                WebhookOutcome(RESULT_ERROR, "verify_failed"),
                transaction_id=transaction_id,
                user_id=user_id,
                resolved_via=resolved_via,
                verify_result="api_error",
                credited_count=0,
            )
            raise
        statuses = payment_statuses(payments_data)

        # --- 5. reconcile (pure)
        creditable = select_creditable_payments(
            payments_data,
            paid_statuses=self._settings.cloudpayments_paid_statuses(),
            now=_now(),
            freshness_hours=self._settings.cloudpayments_payment_freshness_hours,
        )
        refunded = await self._apply_refunds(
            select_refunded_payments(
                payments_data, refunded_statuses=self._settings.cloudpayments_refunded_statuses()
            ),
            user_id,
            resolved_via,
        )
        if not creditable and refunded:
            return self._emit(
                WebhookOutcome(RESULT_APPLIED, "refunded"),
                transaction_id=transaction_id,
                user_id=user_id,
                resolved_via=resolved_via,
                verify_result="ok",
                credited_count=0,
                statuses=statuses,
            )
        if not creditable:
            # A forged callback ends HERE: verify confirmed nothing → zero credits.
            return self._emit(
                WebhookOutcome(RESULT_IGNORED, "no_creditable_payment"),
                transaction_id=transaction_id,
                user_id=user_id,
                resolved_via=resolved_via,
                verify_result="ok",
                credited_count=0,
                statuses=statuses,
            )

        # --- 6. credit each verified payment. Each in its OWN transaction, so one bad product does
        # not roll back a legitimate grant that already happened in this callback.
        credited = 0
        replayed = 0
        skipped = 0
        rejected_reason: str | None = None
        for payment in creditable:
            status, reason = await self._apply_payment(payment, user_id, resolved_via)
            if status == "granted":
                credited += 1
            elif status == "replayed":
                replayed += 1
            elif status == "skipped":
                skipped += 1
            elif status == "rejected":
                # Report the REAL reason: unknown_product and product_not_in_channel are different
                # mis-configurations and lead to different runbooks.
                rejected_reason = reason or "unknown_product"

        if credited:
            outcome = WebhookOutcome(RESULT_APPLIED, "granted")
        elif rejected_reason:
            # Verified money, zero credits — a PRODUCTS mis-configuration. Loud (lost_payment).
            outcome = WebhookOutcome(RESULT_REJECTED, rejected_reason)
        elif skipped:
            # ⚠ OUR OWN verify confirmed the payment, but its product class is one we do not model:
            # zero credits, nothing journalled. Reporting this as a benign "duplicate" would show
            # the on-call a healthy high-volume path while the payer got nothing.
            outcome = WebhookOutcome(RESULT_REJECTED, "unknown_payment_type")
        else:
            # Every verified payment was already credited earlier — a genuine re-delivery.
            outcome = WebhookOutcome(RESULT_DUPLICATE, "duplicate_delivery")
        return self._emit(
            outcome,
            transaction_id=transaction_id,
            user_id=user_id,
            resolved_via=resolved_via,
            verify_result="ok",
            credited_count=credited,
            statuses=statuses,
        )

    async def _apply_refunds(
        self, refunds: list[RefundedPayment], user_id: uuid.UUID, resolved_via: str
    ) -> int:
        """Reverse every CONFIRMED refund of a payment we credited. Returns how many were new.

        Only payments present in OUR ledger are touched, so a user's refund history cannot be
        replayed into anything; each refund is journalled once (``refund:<payment_id>``) and its
        grant reversed once (``refund:cp-txn:<payment_id>``). Each in its own transaction.
        """
        applied = 0
        for refund in refunds:
            grant_key = f"cp-txn:{refund.payment_id}"
            credited = await self._session.scalar(
                text(
                    "SELECT 1 FROM ledger_transactions "
                    "WHERE user_id = :uid AND idempotency_key = :key AND type = 'credit'"
                ),
                {"uid": str(user_id), "key": grant_key},
            )
            if credited is None:
                continue
            outcome = await self._journal.record_refund(
                session=self._session,
                channel=CHANNEL_CLOUDPAYMENTS,
                external_id=f"refund:{refund.payment_id}",
                user_id=user_id,
                product_id=refund.product_code,
                event_type="refund",
                refunded_grant_keys=(grant_key,),
                revoke_subscription=refund.payment_type == parser.PAYMENT_TYPE_SUBSCRIPTION,
                payload={"paymentId": refund.payment_id, "productCode": refund.product_code},
                resolved_via=resolved_via,
            )
            await self._session.commit()
            if outcome.status == "refunded":
                applied += 1
        return applied

    async def _apply_payment(
        self, payment: CreditablePayment, user_id: uuid.UUID, resolved_via: str
    ) -> tuple[str, str | None]:
        """Journal + grant ONE verified payment, then commit. Returns ``(status, reason)``."""
        kind = parser.kind_for_payment_type(payment.payment_type)
        if kind is None:
            # A verified payment of a class we do not model. NOT silently ignorable: the caller
            # turns this into a lost_payment outcome.
            return "skipped", "unknown_payment_type"

        outcome = await self._journal.record_and_grant(
            session=self._session,
            channel=CHANNEL_CLOUDPAYMENTS,
            # external_id = payment_id FROM VERIFY, never the callback's TransactionId.
            external_id=payment.payment_id,
            user_id=user_id,
            product_id=payment.product_code,
            kind=kind,
            event_type=payment.payment_type,
            grant_idempotency_key=f"cp-txn:{payment.payment_id}",
            payload={
                "paymentId": payment.payment_id,
                "productCode": payment.product_code,
                "paymentType": payment.payment_type,
                "status": payment.status,
                "paidAt": payment.paid_at.isoformat(),
            },
            resolved_via=resolved_via,
        )
        # ⚠ ACCESS FOLLOWS THE JOURNAL, and ONLY a real grant extends it.
        # `granted` only — NOT `replayed`: a replay means this payment was already credited, and
        # its access period already granted. Extending on a replay would restart the 30 days on
        # every re-delivery of the same payment, so a chatty aggregator would hand out an ever
        # sliding subscription that was paid for once.
        if kind == parser.KIND_SUBSCRIPTION and outcome.status == "granted":
            await self._upsert_subscription(user_id, payment.product_code, payment.paid_at)
        # Commit AFTER both layers — never between them (that is the lost-payment scenario).
        await self._session.commit()
        return outcome.status, outcome.reason

    async def _upsert_subscription(
        self, user_id: uuid.UUID, plan: str, paid_at: datetime.datetime
    ) -> None:
        """Activate/extend by ONE period of the paid product (``PRODUCTS[plan].period``).

        The aggregator reports a payment, never an expiry, so the term comes from the catalogue: a
        weekly plan buys 7 days, a yearly one a calendar year (a flat 30 days here used to give a
        weekly payer a month and a yearly payer one month). The period starts at the later of the
        payment time and the current still-active expiry — a renewal paid a day early is added to
        the remaining time, not overlapping it.
        """
        product = get_products().get(plan)
        current = await self._session.scalar(
            text(
                "SELECT expires_at FROM subscriptions "
                "WHERE user_id = :uid AND status = 'active' AND expires_at > now()"
            ),
            {"uid": str(user_id)},
        )
        start = max(paid_at, current) if current is not None else paid_at
        if product is not None and product.period:
            expires_at = add_period(start, product.period)
        else:  # pragma: no cover - parse_products refuses a CP subscription without a period
            expires_at = start + datetime.timedelta(days=_SUBSCRIPTION_DEFAULT_DAYS)
        await self._session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at) "
                "VALUES (:uid, 'active', :plan, :expires_at, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET status = 'active', plan = EXCLUDED.plan, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()"
            ),
            {"uid": str(user_id), "plan": plan, "expires_at": expires_at},
        )
