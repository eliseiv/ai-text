"""The ONE exit point of every billing operation: outcome log + metric.

Every billing operation — webhook, subscription sync, token purchase, checkout — emits **exactly
one** structured outcome record and **exactly one** ``billing_outcome_total`` sample, on **every**
exit path, including the early ones that never reach the ``payments`` journal.

Why this module exists at all: in the source, the reason an event was ignored lived ONLY in the
HTTP body returned to the payment platform. The platform does not show it. Our logs had nothing.
A paying user went a month without access and nobody had a single signal. **An endpoint that
answers 200 on failure MUST log the reason, or its failures do not exist.**

`impact` — the label alerts match on
------------------------------------
An alert must match the INVARIANT ("what does this mean for money"), never a regex over today's
list of reasons: a new reason of the same meaning would silently fall out of the alert.

The classification is not a judgement call — it follows a SYMMETRIC predicate:

* **P1 (against under-estimating).** upstream confirmed money AND 0 credits granted ⇒ money is
  lost ⇒ ``lost_payment`` / ``refund_needed`` (WARNING + page).
* **P2 (against over-estimating).** no money taken AND nothing broken ⇒ no action ⇒ ``none``
  (INFO, never page). P2 matters as much as P1: a noisy reason inside an impact class devalues
  the expensive reason of the same class — after two false pages on a routine
  ``unknown_event_type`` the on-call starts ignoring ``BillingUpstreamFailing``, and with it the
  ``invalid_transaction`` spike that means an expired Apple root CA and paying users getting
  nothing.

`impact` is a function of the **exit path** ``(op, reason)``, not of ``reason`` alone: the same
``unknown_product`` means "Apple already took the money" in ``token_purchase`` (verification
happened one step earlier) ⇒ ``lost_payment``, but "no payment exists yet" in ``checkout`` ⇒
``none``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast, get_args

from app.observability.logging import log_event
from app.observability.metrics import billing_outcome_total

logger = logging.getLogger("app.billing.outcome")

# --- reason: a CLOSED enum, not `str` ---------------------------------------------------------
# `reason` is a Prometheus LABEL. Typing it `str` puts no bound on cardinality: one caller passing
# a free-form message (an exception text, an upstream body, an id) would explode the time series
# and take the whole metric down with it. The closed Literal makes mypy reject that at the call
# site, instead of relying on everyone remembering the rule.
BillingReason = Literal[
    # --- StoreKit (subscription_sync / token_purchase) ---
    "verification_unavailable",
    "invalid_transaction",
    "unknown_product",
    "product_not_in_channel",
    "subscription_required",
    # --- webhooks ---
    "user_not_found",
    "missing_customer_user_id",
    "invalid_account_id",
    "missing_transaction_id",
    "unknown_payment_type",
    "unknown_event_type",
    "missing_event_id",
    "invalid_json",
    "not_an_object",
    "empty_body",
    "not_a_completed_payment",
    "verify_failed",
    "no_creditable_payment",
    "not_configured",
    # --- checkout ---
    "upstream_error",
    "created",
    # --- neutral outcomes (success / benign no-op) ---
    "granted",
    "replayed",
    "applied",
    "duplicate_delivery",
    "no_grant",
    "noop",
    "renewal_cancelled",
    "expired",
    # --- refunds ---
    "refunded",
    "refund_without_grant",
    # --- the fallback for a value that is not in this list (see `as_reason`) ---
    "unknown_reason",
]

_KNOWN_REASONS: frozenset[str] = frozenset(get_args(BillingReason))


def as_reason(value: str | None) -> BillingReason | None:
    """Narrow a runtime string (an error `code`, a journal status) to the closed reason enum.

    An unlisted value is NOT passed through to the metric — it is collapsed to ``unknown_reason``
    and logged as an ERROR. Cardinality stays bounded no matter what a caller does, and the
    unclassified exit path is loud rather than silently creating a new time series.
    """
    if value is None:
        return None
    if value in _KNOWN_REASONS:
        return cast(BillingReason, value)
    logger.error(
        "undeclared_billing_reason: %r — add it to BillingReason and to the (op, reason) matrix",
        value,
    )
    return "unknown_reason"


# --- channels -------------------------------------------------------------------------------
CHANNEL_APPLE = "apple_storekit"
CHANNEL_ADAPTY = "adapty"
CHANNEL_CLOUDPAYMENTS = "cloudpayments"

# --- operations (`op`) ----------------------------------------------------------------------
OP_WEBHOOK = "webhook"
OP_SUBSCRIPTION_SYNC = "subscription_sync"
OP_TOKEN_PURCHASE = "token_purchase"
OP_CHECKOUT = "checkout"

# --- results --------------------------------------------------------------------------------
RESULT_APPLIED = "applied"
RESULT_DUPLICATE = "duplicate"
RESULT_IGNORED = "ignored"
RESULT_NOOP = "noop"
RESULT_REJECTED = "rejected"
RESULT_ERROR = "error"

# --- impact (the alerting label) -------------------------------------------------------------
IMPACT_NONE = "none"
IMPACT_LOST_PAYMENT = "lost_payment"  # money confirmed, 0 credits, refusal UNINTENTIONAL → top up
IMPACT_REFUND_NEEDED = "refund_needed"  # money confirmed, 0 credits, refusal INTENTIONAL → refund
IMPACT_UPSTREAM = "upstream"  # could not confirm/complete a probably-paid operation

# Outcomes that are, by definition, a success or a benign no-op — on ANY op.
_NEUTRAL_REASONS = frozenset(
    {
        "granted",
        "replayed",
        "applied",
        "duplicate_delivery",
        "no_grant",
        "created",
        "noop",
        "renewal_cancelled",
        "expired",
        # A refund we applied (credits taken back / access revoked) is a completed operation.
        "refunded",
        # A refund of a purchase we never credited: nothing to take back, nothing lost.
        "refund_without_grant",
    }
)

# THE MATRIX of exit paths. Every (op, reason) pair a billing
# operation can exit through is declared here — an undeclared pair is a bug, not a default.
_IMPACT: dict[tuple[str, str], str] = {
    # --- StoreKit: subscription sync -------------------------------------------------------
    # No Apple root CA mounted → we CANNOT verify. Users pay, credits do not flow.
    (OP_SUBSCRIPTION_SYNC, "verification_unavailable"): IMPACT_UPSTREAM,
    # A forged transaction and a legitimate-but-failing one are INDISTINGUISHABLE → a spike means
    # paying users are getting nothing (e.g. rotated Apple certs), so it must be visible.
    (OP_SUBSCRIPTION_SYNC, "invalid_transaction"): IMPACT_UPSTREAM,
    # Fires AFTER cryptographic verification ⇒ Apple ALREADY took the money, credits are 0.
    (OP_SUBSCRIPTION_SYNC, "unknown_product"): IMPACT_LOST_PAYMENT,
    (OP_SUBSCRIPTION_SYNC, "product_not_in_channel"): IMPACT_LOST_PAYMENT,
    # --- StoreKit: consumable token purchase -----------------------------------------------
    (OP_TOKEN_PURCHASE, "verification_unavailable"): IMPACT_UPSTREAM,
    (OP_TOKEN_PURCHASE, "invalid_transaction"): IMPACT_UPSTREAM,
    (OP_TOKEN_PURCHASE, "unknown_product"): IMPACT_LOST_PAYMENT,
    (OP_TOKEN_PURCHASE, "product_not_in_channel"): IMPACT_LOST_PAYMENT,
    # Policy-guard, AFTER verification: the user paid Apple and we deliberately will NOT credit
    # (credits without a subscription are useless) ⇒ the money must be REFUNDED by hand.
    (OP_TOKEN_PURCHASE, "subscription_required"): IMPACT_REFUND_NEEDED,
    # --- Webhooks (adapty / cloudpayments) --------------------------------------------------
    # The subscription IS paid; we just cannot find whom to credit.
    (OP_WEBHOOK, "user_not_found"): IMPACT_LOST_PAYMENT,
    (OP_WEBHOOK, "missing_customer_user_id"): IMPACT_LOST_PAYMENT,
    # CloudPayments, PUBLIC unsigned callback: it CLAIMS a completed payment but carries an
    # unusable AccountId.
    #
    # money = "claimed", NOT "confirmed": the resolve step sits BEFORE the outgoing verify on
    # purpose (anti-amplification), so nothing of ours has confirmed that any money
    # exists — the verify route is never even called on this path.
    # remediation = "investigate", not "credit_after_fix": there is NO addressee to credit. The
    # AccountId is unreadable, so even after fixing the cause, replaying the callback grants
    # nothing. ⇒ impact = upstream ("verify the money by hand"), NOT lost_payment — the
    # lost_payment response is "top the payer up", and here there is nobody to top up.
    #
    # It is NOT the same class as `missing_customer_user_id`: that one arrives over the
    # AUTHENTICATED M2M channel (Adapty bearer) ⇒ money = "confirmed". Same-looking reason,
    # different exit path, different inputs ⇒ different impact. Reasoning by analogy between the
    # two is exactly what the predicate exists to prevent.
    #
    # Over-estimating here is not harmless (R-OBS-4): the endpoint is PUBLIC, so anyone could
    # raise BillingLostPayment with a single POST and burn the alert that must fire on real losses.
    (OP_WEBHOOK, "invalid_account_id"): IMPACT_UPSTREAM,
    (OP_WEBHOOK, "unknown_product"): IMPACT_LOST_PAYMENT,
    (OP_WEBHOOK, "product_not_in_channel"): IMPACT_LOST_PAYMENT,
    # A GRANTING event carrying NO transaction id: the subscription IS paid (authenticated M2M
    # channel), but no grant key can be built ⇒ nothing is credited, and nothing ever will be.
    # This is precisely the incident class (a payload drift breaks id extraction), so it
    # must be loud. It CANNOT share `no_grant` with EXPIRING/NOOP, where the same label means "no
    # money was involved" — one label would carry two opposite meanings.
    (OP_WEBHOOK, "missing_transaction_id"): IMPACT_LOST_PAYMENT,
    # CloudPayments: OUR OWN verify confirmed the payment, but its product class is one we do not
    # model ⇒ confirmed money, zero credits, nothing journalled. Not a "duplicate".
    (OP_WEBHOOK, "unknown_payment_type"): IMPACT_LOST_PAYMENT,
    # A payload-format drift loses EVERY grant of the channel — broken, and money is flowing.
    (OP_WEBHOOK, "invalid_json"): IMPACT_UPSTREAM,
    (OP_WEBHOOK, "not_an_object"): IMPACT_UPSTREAM,
    (OP_WEBHOOK, "missing_event_id"): IMPACT_UPSTREAM,
    # CloudPayments: the aggregator did not confirm the payment we were triggered about.
    (OP_WEBHOOK, "verify_failed"): IMPACT_UPSTREAM,
    (OP_WEBHOOK, "no_creditable_payment"): IMPACT_UPSTREAM,
    # The channel is not configured (no API token) ⇒ we cannot verify ⇒ we cannot credit a payment
    # the aggregator is telling us about. Our configuration, our fault.
    (OP_WEBHOOK, "not_configured"): IMPACT_UPSTREAM,
    # A platform event type we do not model: a routine no-op of the platform, NOT a payment.
    # Paging on it would burn the alert that must fire on invalid_transaction (P2).
    (OP_WEBHOOK, "unknown_event_type"): IMPACT_NONE,
    (OP_WEBHOOK, "empty_body"): IMPACT_NONE,  # the platform's connectivity ping
    # Status != Completed / OperationType != Payment: no money was taken.
    (OP_WEBHOOK, "not_a_completed_payment"): IMPACT_NONE,
    # --- Checkout (outgoing payment link) ---------------------------------------------------
    # The aggregator is down: nothing to pay WITH — broken, but no money was taken yet.
    (OP_CHECKOUT, "upstream_error"): IMPACT_UPSTREAM,
    # No payment exists yet ⇒ nothing is lost. The user sees the 422/503 immediately.
    (OP_CHECKOUT, "unknown_product"): IMPACT_NONE,
    (OP_CHECKOUT, "product_not_in_channel"): IMPACT_NONE,
    (OP_CHECKOUT, "not_configured"): IMPACT_NONE,
}


def impact_for(op: str, reason: str | None) -> str:
    """Resolve the alerting impact of an exit path. Undeclared pair ⇒ loud, conservative fallback.

    A success / benign no-op is ``none`` on any op. An undeclared pair means a developer added an
    exit path without classifying it: we do NOT silently call it ``none`` (that would hide a lost
    payment), we return ``upstream`` and log an ERROR so it is noticed. The invariant test asserts
    every emitted pair is declared, so this fallback should never fire in a healthy build.
    """
    if reason is None or reason in _NEUTRAL_REASONS:
        return IMPACT_NONE
    declared = _IMPACT.get((op, reason))
    if declared is None:
        logger.error(
            "undeclared_billing_impact: op=%s reason=%s — classify it in billing/outcome.py",
            op,
            reason,
        )
        return IMPACT_UPSTREAM
    return declared


def _level_for(impact: str, result: str) -> int:
    """WARNING is what the on-call sees. Reserve it for money that is (or may be) lost."""
    if result == RESULT_ERROR:
        return logging.ERROR
    if impact in (IMPACT_LOST_PAYMENT, IMPACT_REFUND_NEEDED, IMPACT_UPSTREAM):
        return logging.WARNING
    return logging.INFO


def emit_billing_outcome(
    *,
    event: str,
    channel: str,
    op: str,
    result: str,
    # A CLOSED enum, not `str`: this value becomes a Prometheus label (see BillingReason). Runtime
    # strings (an exception `code`, a journal status) go through `as_reason()` first.
    reason: BillingReason | None = None,
    **fields: Any,
) -> str:
    """Emit THE outcome log + THE metric sample of one exit path. Returns the resolved ``impact``.

    Call this EXACTLY ONCE per billing operation, on EVERY exit path. ``fields`` is the
    per-event allowlist — the raw payload, bearer secrets, card PII,
    ``amount``/``currency`` and ``customerEmail`` must never be passed in.
    """
    impact = impact_for(op, reason)
    billing_outcome_total.labels(
        channel=channel,
        op=op,
        result=result,
        impact=impact,
        reason=reason or "none",
    ).inc()
    log_event(
        logger,
        _level_for(impact, result),
        event,
        result=result,
        reason=reason,
        channel=channel,
        op=op,
        impact=impact,
        **fields,
    )
    return impact
