"""Policy state loading (the ONLY I/O of the module) + domain gates.

``engine.evaluate()`` stays pure; everything that touches the database lives here.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.loader import load_registry
from app.models import Subscription, User, Wallet
from app.policy.engine import (
    BillingKind,
    BlockReason,
    Decision,
    PolicyState,
    SubscriptionStatus,
    evaluate,
)


@dataclass(frozen=True)
class EffectivePolicy:
    """What ``GET /v1/policy/effective`` answers — the same verdict the generation would give."""

    allowed: bool
    reasons: list[BlockReason]
    subscription_status: SubscriptionStatus
    credits_balance: int
    trial_used: bool
    required_credits: int
    billing_kind: BillingKind = BillingKind.none


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def effective_subscription_status(
    status: str | None, expires_at: datetime.datetime | None
) -> SubscriptionStatus:
    """LAZY EXPIRY: there is no background job — expiry is computed ON READ.

    Consequences (important and non-obvious):
      * an "expired" subscription still has ``status='active'`` IN THE DATABASE until someone
        reads it through this function. Any direct SQL against ``subscriptions`` sees the WRONG
        status — a trap for analytics;
      * anything that sets ``expires_at`` must set it strictly ``> now()``, otherwise the grant is
        useless.

    This function does NOT mutate the row: reading must never write on a hot path.
    """
    if status is None or status == "none":
        return SubscriptionStatus.none
    if status == "expired":
        return SubscriptionStatus.expired
    if expires_at is not None and expires_at <= _now():
        return SubscriptionStatus.expired
    return SubscriptionStatus.active


async def load_policy_state(session: AsyncSession, user_id: uuid.UUID) -> PolicyState:
    """Read subscription / wallet / trial flag into the pure state object."""
    user = await session.get(User, user_id)
    trial_used = bool(user.trial_used) if user is not None else False

    sub = await session.scalar(select(Subscription).where(Subscription.user_id == user_id))
    sub_status = effective_subscription_status(
        sub.status if sub else None, sub.expires_at if sub else None
    )

    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    balance = int(wallet.balance) if wallet is not None else 0

    return PolicyState(
        subscription_status=sub_status,
        trial_used=trial_used,
        credits_balance=balance,
    )


def apply_gates(
    decision: Decision,
    state: PolicyState,
    ctx: Mapping[str, Any] | None = None,
) -> Decision:
    """Run the domain gates AFTER the core decision. Gates may only TIGHTEN it.

    A gate returning ``allowed`` where the core said ``blocked`` is IGNORED — a domain must not be
    able to hand out access the core denied, or it would bypass billing entirely. The first
    blocking gate wins (the chain stops).
    """
    context: Mapping[str, Any] = ctx or {}
    for gate in load_registry().policy_gates:
        override = gate.check(state, context)
        if override is not None and not override.allowed:
            return override
    return decision


async def effective(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    required_credits: int = 1,
    ctx: Mapping[str, Any] | None = None,
) -> EffectivePolicy:
    """``GET /v1/policy/effective`` — the SAME ``evaluate()`` the generation runs (BR-POL-6).

    Consistency between the UI and the generation is guaranteed by construction (one function),
    not by discipline: a second, "UI-only" implementation would drift apart the first time either
    is touched.
    """
    state = await load_policy_state(session, user_id)
    decision = apply_gates(evaluate(state, required_credits=required_credits), state, ctx)

    reasons: list[BlockReason] = []
    if not decision.allowed and decision.block_reason is not None:
        reasons.append(decision.block_reason)

    return EffectivePolicy(
        allowed=decision.allowed,
        reasons=reasons,
        subscription_status=state.subscription_status,
        credits_balance=state.credits_balance,
        trial_used=state.trial_used,
        required_credits=required_credits,
        billing_kind=decision.billing_kind,
    )


__all__ = [
    "EffectivePolicy",
    "effective_subscription_status",
    "apply_gates",
    "effective",
    "load_policy_state",
]
