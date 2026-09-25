"""Pure access-policy state machine.

``evaluate()`` is the SINGLE source of truth of access rules: both ``POST /v1/generate`` and
``GET /v1/policy/effective`` call this one function, so the UI can never say "you may" while the
generation says "blocked" (BR-POL-6).

**Pure by design** — no DB, no Redis, no clock, no logging, no mutation. The full transition table
is the cartesian product {none, active, expired} × {trial_used} × {balance vs required}; a pure
function lets it be covered exhaustively by parametrized unit tests WITHOUT a database. Had
``evaluate()`` done I/O, full coverage would have cost an integration test per combination — and
therefore would not exist.

No BYOK here: that was an LLM-chat domain feature. A domain needing extra checks registers a
``PolicyGate`` via ``DomainRegistry.policy_gates`` — gates may only TIGHTEN the decision.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    none = "none"


class BillingKind(str, enum.Enum):
    """How the next generation would be paid for (mirrors ``generation_billing_kind`` in the DB)."""

    none = "none"
    credits = "credits"
    trial = "trial"
    unbilled = "unbilled"


class BlockReason(str, enum.Enum):
    trial_used = "trial_used"
    subscription_required = "subscription_required"
    subscription_expired = "subscription_expired"
    credits_empty = "credits_empty"
    policy_denied = "policy_denied"
    # Gateway concern (HTTP 429). `evaluate()` NEVER returns it and /policy/effective never lists
    # it — Policy does not know the state of the Redis limiter. It exists in the enum only because
    # the HTTP layer shares the blockReason vocabulary.
    rate_limited = "rate_limited"


@dataclass(frozen=True)
class PolicyState:
    subscription_status: SubscriptionStatus
    trial_used: bool
    credits_balance: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    block_reason: BlockReason | None = None
    billing_kind: BillingKind = BillingKind.none

    @staticmethod
    def allow(billing_kind: BillingKind) -> Decision:
        return Decision(allowed=True, block_reason=None, billing_kind=billing_kind)

    @staticmethod
    def block(reason: BlockReason) -> Decision:
        return Decision(allowed=False, block_reason=reason, billing_kind=BillingKind.none)


def evaluate(state: PolicyState, *, required_credits: int = 1) -> Decision:
    """Decide access for a generation costing ``required_credits``. Pure.

    BRANCH ORDER IS LOAD-BEARING — subscription is checked BEFORE balance:
    a user with an expired subscription and 1000 credits is BLOCKED (BR-POL-3). A subscription is
    the RIGHT, credits are the RESOURCE; both are required. This is why an admin compensation via
    ``wallet/grant`` alone is not enough — the subscription must be activated too.

    ``required_credits`` is a PARAMETER, not the constant 1 (BR-POL-7): with units/tokens pricing
    a balance of 1 and a price of 3 must block BEFORE the provider is called — otherwise the
    upstream would run (real money spent) and the debit would then find no credits, forcing either
    a negative balance (forbidden by CHECK) or a free generation.
    """
    if state.subscription_status is SubscriptionStatus.active:
        if state.credits_balance >= required_credits:
            return Decision.allow(BillingKind.credits)
        return Decision.block(BlockReason.credits_empty)

    if state.subscription_status is SubscriptionStatus.expired:
        return Decision.block(BlockReason.subscription_expired)

    # subscription_status is none
    if state.trial_used:
        return Decision.block(BlockReason.trial_used)
    return Decision.allow(BillingKind.trial)  # the single lifetime trial (BR-1), free
