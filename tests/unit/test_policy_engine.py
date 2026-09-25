"""Policy state machine — the FULL transition table (AC-1, AC-2, AC-7).

``evaluate()`` is pure, so the whole cartesian product
{none, active, expired} × {trial_used} × {balance} × {required_credits} is covered by
parametrized unit tests without a database. That is the payoff of keeping the I/O out of it.
"""

from __future__ import annotations

import itertools

import pytest

from app.policy.engine import (
    BillingKind,
    BlockReason,
    Decision,
    PolicyState,
    SubscriptionStatus,
    evaluate,
)


def _expected(
    status: SubscriptionStatus, trial_used: bool, balance: int, required: int
) -> Decision:
    """The rules, restated INDEPENDENTLY of the implementation.

    Branch order is load-bearing: subscription is the RIGHT, credits are the RESOURCE — an expired
    subscription blocks even with 1000 credits (BR-POL-3).
    """
    if status is SubscriptionStatus.active:
        if balance >= required:
            return Decision.allow(BillingKind.credits)
        return Decision.block(BlockReason.credits_empty)
    if status is SubscriptionStatus.expired:
        return Decision.block(BlockReason.subscription_expired)
    if trial_used:
        return Decision.block(BlockReason.trial_used)
    return Decision.allow(BillingKind.trial)


_CASES = list(
    itertools.product(
        list(SubscriptionStatus),
        [False, True],
        [0, 1, 2, 3, 1000],
        [1, 3],
    )
)


@pytest.mark.parametrize(("status", "trial_used", "balance", "required"), _CASES)
def test_evaluate_full_transition_table(
    status: SubscriptionStatus, trial_used: bool, balance: int, required: int
) -> None:
    state = PolicyState(subscription_status=status, trial_used=trial_used, credits_balance=balance)
    assert evaluate(state, required_credits=required) == _expected(
        status, trial_used, balance, required
    )


def test_expired_subscription_blocks_even_with_a_large_balance() -> None:
    state = PolicyState(SubscriptionStatus.expired, trial_used=False, credits_balance=10_000)
    decision = evaluate(state, required_credits=1)
    assert decision.allowed is False
    assert decision.block_reason is BlockReason.subscription_expired


def test_balance_below_required_credits_blocks_before_the_provider_is_called() -> None:
    # BR-POL-7: balance 1, price 3 → blocked. "balance > 0" is the wrong question with units/tokens
    # pricing: the upstream would run (real money) and the debit would then find nothing.
    state = PolicyState(SubscriptionStatus.active, trial_used=True, credits_balance=1)
    decision = evaluate(state, required_credits=3)
    assert decision.allowed is False
    assert decision.block_reason is BlockReason.credits_empty


def test_trial_is_allowed_once_and_is_free() -> None:
    fresh = PolicyState(SubscriptionStatus.none, trial_used=False, credits_balance=0)
    used = PolicyState(SubscriptionStatus.none, trial_used=True, credits_balance=0)
    assert evaluate(fresh) == Decision.allow(BillingKind.trial)
    assert evaluate(used) == Decision.block(BlockReason.trial_used)


def test_blocked_decision_never_carries_a_billing_kind() -> None:
    for status, trial_used, balance in itertools.product(
        list(SubscriptionStatus), [False, True], [0, 5]
    ):
        decision = evaluate(
            PolicyState(status, trial_used=trial_used, credits_balance=balance), required_credits=3
        )
        if not decision.allowed:
            assert decision.billing_kind is BillingKind.none


def test_rate_limited_is_never_a_policy_verdict() -> None:
    # It is a gateway concern (HTTP 429): Policy does not know the state of the Redis limiter.
    for status, trial_used, balance, required in _CASES:
        decision = evaluate(
            PolicyState(status, trial_used=trial_used, credits_balance=balance),
            required_credits=required,
        )
        assert decision.block_reason is not BlockReason.rate_limited
