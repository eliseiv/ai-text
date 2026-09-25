"""Append-only audit journal.

Only INSERT — never UPDATE/DELETE from code (DB-level enforcement: TD-001). The payload is
redacted before insert: ``assert_no_secrets()`` is the LAST guard before the
row lands in ``audit_logs``.

**Transactional property (the point of this module):** ``AuditService`` never opens its own
transaction — the CALLER passes its session, so the money action and the record about it commit or
roll back TOGETHER. "Debited but not recorded" and "recorded but not debited" are both impossible
states; audit completeness is a guarantee, not best-effort (unlike logs).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.observability.context import get_request_id
from app.observability.redaction import assert_no_secrets

# eventType catalogue of the CORE.
# These strings are PERSISTED in audit_logs.event_type: ops queries, tests (AC-8) and every later
# phase (wallet / generation / payments / auth / admin) key off them. Renaming one is a breaking
# change of the data, not a refactor.
EVENT_BILLING_DEBIT = "billing_debit"  # Wallet: amount, reason, ledgerTxId
EVENT_BILLING_CREDIT = "billing_credit"  # Wallet: amount, reason, ledgerTxId
EVENT_POLICY_BLOCKED = "policy_blocked"  # Generation: blockReason, requiredCredits, balance
EVENT_GENERATION_SUCCEEDED = "generation_succeeded"  # kind, provider, model, creditsCharged, units
EVENT_GENERATION_FAILED = "generation_failed"  # kind, provider, errorCode
EVENT_PAYMENT_EVENT = "payment_event"  # PaymentsJournal: channel, kind, status, productId, …
EVENT_PAYMENT_REFUND = "payment_refund"  # PaymentsJournal: channel, creditsRevoked, ...
EVENT_ADMIN_GRANT = "admin_grant"  # credits, idempotencyKey, reason
EVENT_ADMIN_SUBSCRIPTION_GRANT = "admin_subscription_grant"  # plan, expiresAt, credits, …
EVENT_AUTH_EVENT = "auth_event"  # action, deviceId

# `action` values carried by EVENT_AUTH_EVENT.
AUTH_ACTION_REFRESH_CHAIN_REVOKED = "refresh_chain_revoked"
AUTH_ACTION_APPLE_IDENTITY_LINKED = "apple_identity_linked"


class AuditService:
    """Records audit events into the CALLER's session (same transaction as the action).

    Stateless on purpose: the session is a per-call argument, so one instance can never smuggle a
    foreign transaction into a caller's
    unit of work.
    """

    async def log(
        self,
        event_type: str,
        *,
        session: AsyncSession,
        user_id: uuid.UUID,
        payload: dict[str, Any],
        generation_id: uuid.UUID | None = None,
    ) -> None:
        """Append one audit record. ``payload`` is an allowlist of fields — never secrets.

        The redaction pass is defensive (callers are expected to pass an allowlist already): it is
        the single guard standing between a mistake upstream and card-PII / secrets persisted in
        the database forever.
        """
        redacted = assert_no_secrets({**payload, "requestId": get_request_id()})
        row = AuditLog(
            user_id=user_id,
            generation_id=generation_id,
            event_type=event_type,
            payload=redacted,
        )
        session.add(row)
        await session.flush()
