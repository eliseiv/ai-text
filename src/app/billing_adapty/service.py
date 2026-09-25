"""Adapty subscription webhook — the MAIN billing path for subscriptions.

``POST /v1/subscription/sync`` is the client path, and it is blind exactly where it matters:
auto-renewal happens WITHOUT the app. If the user does not open the app for a month, the server
never learns about the renewal, ``expires_at`` lapses, and a PAYING customer loses access. Same for
cancellation and refunds. Hence: the webhook is the primary path, ``sync`` is the fallback.

Two HTTP rules that look wrong and are not:

* **``200 ignored`` on any garbage — after successful authorization.** Adapty retries every non-2xx
  FOREVER, and refuses to even save the webhook config if the verification ping is not 2xx. A
  Pydantic body model would answer 422 to that ping ⇒ the webhook could never be configured, and a
  format drift would become an infinite retry storm.
* **``5xx`` only on a REAL failure** (DB down), where a retry actually helps — the transaction rolls
  back and reprocessing is clean.

The difference between ``200 ignored`` and ``5xx`` is "would a retry fix it". This is not
swallowing errors — which is why every path emits an outcome log + metric: in the source
the reason lived only in the HTTP body Adapty never shows, and a real incident lived unseen.
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
    CHANNEL_ADAPTY,
    OP_WEBHOOK,
    RESULT_APPLIED,
    RESULT_DUPLICATE,
    RESULT_IGNORED,
    RESULT_NOOP,
    RESULT_REJECTED,
    as_reason,
    emit_billing_outcome,
)
from app.billing.payments import (
    KIND_SUBSCRIPTION,
    KIND_SUBSCRIPTION_EVENT,
    KIND_TOKENS,
    PaymentsJournal,
    apple_grant_keys,
)
from app.billing_adapty import parser
from app.billing_adapty.parser import ParsedEvent
from app.billing_common.resolve import resolve_user

_OUTCOME_EVENT = "adapty_webhook_outcome"


@dataclass(frozen=True)
class WebhookOutcome:
    """Mapped to HTTP 200 by the router (except a real failure, which raises)."""

    result: str
    reason: str | None = None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class AdaptyWebhookService:
    def __init__(self, session: AsyncSession, journal: PaymentsJournal) -> None:
        self._session = session
        self._journal = journal

    def _emit(
        self,
        outcome: WebhookOutcome,
        *,
        event_type: str | None = None,
        event_id: str | None = None,
        customer_user_id: str | None = None,
        resolved_user_id: uuid.UUID | None = None,
        resolved_via: str | None = None,
    ) -> WebhookOutcome:
        """EXACTLY ONE outcome record per call, on EVERY exit path. Strict field allowlist —
        the raw payload and the bearer secret are never logged."""
        emit_billing_outcome(
            event=_OUTCOME_EVENT,
            channel=CHANNEL_ADAPTY,
            op=OP_WEBHOOK,
            result=outcome.result,
            reason=as_reason(outcome.reason),
            eventType=event_type,
            eventId=event_id,
            customerUserId=customer_user_id,
            resolvedUserId=str(resolved_user_id) if resolved_user_id else None,
            resolvedVia=resolved_via,
        )
        return outcome

    async def handle(self, raw: bytes) -> WebhookOutcome:
        """Parse → resolve → classify → journal.

        Never raises on a bad payload; a real DB failure propagates (→ 500, and a retry is wanted).
        """
        # --- body shape (no DB) ---
        if not raw:
            return self._emit(WebhookOutcome(RESULT_IGNORED, "empty_body"))
        try:
            body: Any = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._emit(WebhookOutcome(RESULT_IGNORED, "invalid_json"))
        if not isinstance(body, dict):
            return self._emit(WebhookOutcome(RESULT_IGNORED, "not_an_object"))

        event_id = parser.parse_event_id(body)
        if event_id is None:
            return self._emit(WebhookOutcome(RESULT_IGNORED, "missing_event_id"))

        # Parse the type BEFORE the identifier check, so the WARNING says "trial_started arrived
        # with no customer_user_id" instead of a faceless reason.
        event_type = parser.parse_event_type(body)
        customer_user_id = parser.parse_customer_user_id(body)
        if customer_user_id is None:
            return self._emit(
                WebhookOutcome(RESULT_IGNORED, "missing_customer_user_id"),
                event_type=event_type or None,
                event_id=event_id,
            )

        # --- user resolution: the SHARED one. Never a channel-local copy (that is how the same
        # bug shipped to prod twice). Users are NEVER provisioned here. ---
        resolved = await resolve_user(self._session, customer_user_id)
        if resolved is None:
            return self._emit(
                WebhookOutcome(RESULT_IGNORED, "user_not_found"),
                event_type=event_type,
                event_id=event_id,
                customer_user_id=customer_user_id,
            )
        user_id, resolved_via = resolved

        if event_type not in parser.KNOWN_EVENTS:
            # A platform no-op we do not model. INFO, impact=none: paging on it would burn the
            # alert that must fire on real money loss.
            return self._emit(
                WebhookOutcome(RESULT_IGNORED, "unknown_event_type"),
                event_type=event_type,
                event_id=event_id,
                customer_user_id=customer_user_id,
                resolved_user_id=user_id,
                resolved_via=resolved_via,
            )

        event = ParsedEvent(
            event_id=event_id,
            event_type=event_type,
            customer_user_id=customer_user_id,
            vendor_product_id=parser.parse_vendor_product_id(body),
            expires_at=parser.parse_expires_at(body),
            transaction_id=parser.parse_transaction_id(body),
            original_transaction_id=parser.parse_original_transaction_id(body),
            is_active=parser.parse_is_active(body),
            access_level_id=parser.parse_access_level_id(body),
            will_renew=parser.parse_will_renew(body),
        )
        return await self._apply(event, user_id, resolved_via)

    async def _apply(
        self, event: ParsedEvent, user_id: uuid.UUID, resolved_via: str
    ) -> WebhookOutcome:
        semantics = parser.classify_event(event)
        product_id = event.vendor_product_id
        txn = event.transaction_id or event.original_transaction_id

        if semantics == parser.SEM_REFUND:
            return await self._refund(event, user_id, resolved_via, txn)

        granting = semantics in (parser.SEM_GRANTING, parser.SEM_TOKENS)
        if granting and not txn:
            # A granting event with NO transaction id: the subscription IS paid, but there is no
            # key to grant under — and there never will be. Journal it as a non-granting event and
            # report it as LOST money (the incident class), not as a routine `no_grant`.
            # The subscription is NOT activated here: access follows a grant we could not make.
            await self._journal.record_and_grant(
                session=self._session,
                channel=CHANNEL_ADAPTY,
                external_id=event.event_id,
                user_id=user_id,
                product_id=None,
                kind=KIND_SUBSCRIPTION_EVENT,
                event_type=event.event_type,
                grant_idempotency_key=None,
                payload=self._payload(event),
                resolved_via=resolved_via,
            )
            return self._emit(
                WebhookOutcome(RESULT_REJECTED, "missing_transaction_id"),
                event_type=event.event_type,
                event_id=event.event_id,
                customer_user_id=event.customer_user_id,
                resolved_user_id=user_id,
                resolved_via=resolved_via,
            )

        if semantics == parser.SEM_GRANTING:
            kind = KIND_SUBSCRIPTION
            # THE GRANT KEY: the per-period Apple transaction id — NOT the per-event id (one
            # purchase emits several events; keying on the event id doubles the balance). The
            # SAME key as StoreKit sync, so the purchase is credited once across both channels.
            grant_key = f"sub-grant:{txn}"
        elif semantics == parser.SEM_TOKENS:
            kind = KIND_TOKENS
            grant_key = f"token-purchase:{txn}"
        else:
            # EXPIRING / NOOP: a valid event that grants nothing by its nature.
            kind = KIND_SUBSCRIPTION_EVENT
            grant_key = None

        outcome = await self._journal.record_and_grant(
            session=self._session,
            channel=CHANNEL_ADAPTY,
            # DELIVERY key = the event id (2 events of one period → 2 payments rows).
            external_id=event.event_id,
            user_id=user_id,
            product_id=product_id,
            kind=kind,
            event_type=event.event_type,
            grant_idempotency_key=grant_key,
            payload=self._payload(event),
            resolved_via=resolved_via,
        )

        # ⚠ ACCESS FOLLOWS THE JOURNAL, not the event. A payment the system officially REJECTED
        # (unknown product / product of another channel) must not hand out a subscription: the
        # right of access would then rest on a payment we refused to credit.
        if semantics == parser.SEM_GRANTING and outcome.status in ("granted", "replayed"):
            await self._upsert_subscription(
                user_id, status="active", plan=product_id, expires_at=event.expires_at
            )
        elif semantics == parser.SEM_EXPIRING and outcome.status != "rejected":
            await self._upsert_subscription(user_id, status="expired", plan=None, expires_at=None)
        # NOOP: auto-renew off — access is KEPT (the period is paid for). Nothing to change.

        result, reason = self._map(semantics, outcome.status, outcome.reason)
        return self._emit(
            WebhookOutcome(result, reason),
            event_type=event.event_type,
            event_id=event.event_id,
            customer_user_id=event.customer_user_id,
            resolved_user_id=user_id,
            resolved_via=resolved_via,
        )

    async def _refund(
        self, event: ParsedEvent, user_id: uuid.UUID, resolved_via: str, txn: str | None
    ) -> WebhookOutcome:
        """The store refunded the purchase: take back its credits; a refunded subscription
        also loses access. Idempotent per event and per refunded grant."""
        outcome = await self._journal.record_refund(
            session=self._session,
            channel=CHANNEL_ADAPTY,
            external_id=event.event_id,
            user_id=user_id,
            product_id=event.vendor_product_id,
            event_type=event.event_type,
            refunded_grant_keys=apple_grant_keys(txn) if txn else (),
            revoke_subscription=event.event_type == "subscription_refunded",
            payload=self._payload(event),
            resolved_via=resolved_via,
        )
        if outcome.status == "replayed":
            result, reason = RESULT_DUPLICATE, "duplicate_delivery"
        elif outcome.grants_found or outcome.subscription_revoked:
            result, reason = RESULT_APPLIED, "refunded"
        else:
            result, reason = RESULT_NOOP, "refund_without_grant"
        return self._emit(
            WebhookOutcome(result, reason),
            event_type=event.event_type,
            event_id=event.event_id,
            customer_user_id=event.customer_user_id,
            resolved_user_id=user_id,
            resolved_via=resolved_via,
        )

    @staticmethod
    def _payload(event: ParsedEvent) -> dict[str, Any]:
        return {
            "profileEventId": event.event_id,
            "eventType": event.event_type,
            "transactionId": event.transaction_id,
            "vendorProductId": event.vendor_product_id,
            "isActive": event.is_active,
            "expiresAt": event.expires_at.isoformat() if event.expires_at else None,
        }

    @staticmethod
    def _map(semantics: str, status: str, journal_reason: str | None) -> tuple[str, str | None]:
        """Journal status → the HTTP-level result/reason pair (also the metric labels)."""
        if status == "rejected":
            # Unknown product / product of another channel: the subscription IS paid ⇒ lost money.
            return RESULT_REJECTED, journal_reason
        if status == "replayed":
            return RESULT_DUPLICATE, journal_reason or "replayed"
        if semantics == parser.SEM_NOOP:
            return RESULT_NOOP, "renewal_cancelled"
        if status == "no_grant":
            return RESULT_APPLIED, "no_grant"
        return RESULT_APPLIED, "granted"

    async def _upsert_subscription(
        self,
        user_id: uuid.UUID,
        *,
        status: str,
        plan: str | None,
        expires_at: datetime.datetime | None,
    ) -> None:
        """EXPIRING keeps ``plan``/``expires_at`` (history), GRANTING overwrites them."""
        if status == "active":
            await self._session.execute(
                text(
                    "INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at) "
                    "VALUES (:uid, 'active', :plan, :expires_at, now()) "
                    "ON CONFLICT (user_id) DO UPDATE SET status = 'active', "
                    "plan = EXCLUDED.plan, expires_at = EXCLUDED.expires_at, updated_at = now()"
                ),
                {"uid": str(user_id), "plan": plan, "expires_at": expires_at},
            )
        else:
            await self._session.execute(
                text(
                    "INSERT INTO subscriptions (user_id, status, updated_at) "
                    "VALUES (:uid, 'expired', now()) "
                    "ON CONFLICT (user_id) DO UPDATE SET status = 'expired', updated_at = now()"
                ),
                {"uid": str(user_id)},
            )
