"""``impact`` of the generation circuit — the label alerts match on (R-OBS-1..6).

The same construction as billing, a different circuit. It matters MORE here, because this is the
point of extension people actually use: every service built from this template brings its own
``policy_gates`` with its own block reasons. Without an invariant label, each of those reasons
would land outside every alert automatically.

**Value in this circuit is lost by one of two sides** — hence the values:

* ``revenue_loss`` — the SERVICE lost: the provider delivered (``status='succeeded'``) but nothing
  was charged and it was not a trial ⇒ ``billing_kind='unbilled'``. We worked for free. This is
  CORRECT behaviour (an under-estimating ``quote()`` let the generation through, the provider ran,
  and the debit found no credits — the user must not go negative), but it used to be completely
  UNOBSERVABLE: the service could give results away for months and nobody would know;
* ``user_blocked`` — the USER lost: a generation stuck in ``running`` (the process died between the
  anchor and the finalization) means his ``Idempotency-Key`` now answers ``409`` forever;
* ``upstream`` — both lost: ``ProviderError``;
* ``none`` — nobody lost: a policy block BEFORE the provider call (the user paid nothing and got
  nothing), a charged success, a trial, a replay, ``too_many_inflight``.

R-OBS-6 holds BY CONSTRUCTION: every input is a column of ``generations`` (or is computed from it),
so a test derives them FROM THE DATABASE — never from a table in the docs.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Mapping

logger = logging.getLogger("app.generation.impact")

IMPACT_NONE = "none"
IMPACT_REVENUE_LOSS = "revenue_loss"
IMPACT_USER_BLOCKED = "user_blocked"
IMPACT_UPSTREAM = "upstream"

BILLING_KIND_UNBILLED = "unbilled"


def generation_impact(
    *,
    status: str,
    credits_charged: int,
    billing_kind: str,
    stuck: bool,
) -> str:
    """TOTAL function over columns of ``generations``. Never a judgement call.

    ``credits_charged`` is part of the signature because ``unbilled`` is defined by it: the DB
    invariant ``credits_charged = 0 OR status='succeeded'`` plus ``billing_kind='unbilled'`` is
    exactly "delivered, charged nothing, and it was not the free trial".
    """
    if stuck:
        return IMPACT_USER_BLOCKED
    if status == "succeeded" and billing_kind == BILLING_KIND_UNBILLED and credits_charged == 0:
        return IMPACT_REVENUE_LOSS
    if status == "failed":
        return IMPACT_UPSTREAM
    return IMPACT_NONE


def is_stuck(
    status: str,
    created_at: datetime.datetime,
    *,
    now: datetime.datetime,
    timeout_seconds: float,
) -> bool:
    """``stuck`` is computed, not judged: unfinished for longer than 2× the provider deadline."""
    if status not in ("pending", "running"):
        return False
    return (now - created_at).total_seconds() > 2 * timeout_seconds


# --- Block reasons → impact (R-OBS-5) --------------------------------------------------------
# A policy block BEFORE the provider call costs nobody anything: the user paid nothing and got
# nothing. Hence `none` for every CORE reason.
_CORE_BLOCK_IMPACTS: dict[str, str] = {
    "trial_used": IMPACT_NONE,
    "subscription_required": IMPACT_NONE,
    "subscription_expired": IMPACT_NONE,
    "credits_empty": IMPACT_NONE,
    "policy_denied": IMPACT_NONE,
}


def block_impact(reason: str, declared: Mapping[str, str] | None = None) -> str:
    """Impact of a block reason. A DOMAIN gate MUST declare the impact of ITS reasons (R-OBS-5).

    This is a CONTRACT of the extension point, not an option: ``policy_gates`` is the mechanism
    every template-born service uses, and an undeclared reason would silently sit outside every
    alert. A gate that rejects an ALREADY PAID operation must NOT return ``none`` — that is exactly
    the ``subscription_required`` defect from billing, where a refusal after payment was labelled
    "routine" and stayed unalerted.

    Undeclared reason at runtime ⇒ loud ERROR + the conservative ``user_blocked`` (somebody is
    being refused and we do not know that it is harmless). The invariant test fails on it.
    """
    if reason in _CORE_BLOCK_IMPACTS:
        return _CORE_BLOCK_IMPACTS[reason]
    if declared and reason in declared:
        return declared[reason]
    logger.error(
        "undeclared_block_impact: reason=%s — a domain PolicyGate must declare the impact of its "
        "block reasons (R-OBS-5)",
        reason,
    )
    return IMPACT_USER_BLOCKED
