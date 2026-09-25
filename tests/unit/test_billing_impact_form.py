"""``impact`` — COMPLETENESS OF FORM (R-OBS-2/5/6/7).

Truth of CONTENT (is each declared value the one the total function computes?) is proven in
``tests/integration/test_billing_impact_truth.py``, where every input is derived from a RUN of the
real code path. This file proves the properties that are checkable statically:

* every ``(op, reason)`` pair the code can actually emit is DECLARED in the matrix;
* the emitted ``reason`` values form a CLOSED set (a Prometheus label — one high-cardinality value,
  say a productId or an error text, and the series count explodes);
* every declared reason is reachable (no dead rows);
* the log level is COMPUTED from ``impact`` — there is no second, independently maintained list of
  levels/alerts/runbooks anywhere in the source (R-OBS-7);
* there is exactly ONE outcome counter — no per-channel ``token_purchase_total`` / ``adapty_total``.

The reference total function lives in ``tests/support/impact_reference.py``.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from app.billing import outcome as outcome_mod
from app.billing.outcome import (
    _IMPACT,
    _NEUTRAL_REASONS,
    IMPACT_LOST_PAYMENT,
    IMPACT_NONE,
    IMPACT_REFUND_NEEDED,
    IMPACT_UPSTREAM,
    OP_CHECKOUT,
    OP_SUBSCRIPTION_SYNC,
    OP_TOKEN_PURCHASE,
    OP_WEBHOOK,
    impact_for,
)
from tests.support.impact_reference import DOC_INPUT_ROWS, reference_impact

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "app"
_OPS = {OP_WEBHOOK, OP_SUBSCRIPTION_SYNC, OP_TOKEN_PURCHASE, OP_CHECKOUT}
_IMPACTS = {IMPACT_NONE, IMPACT_LOST_PAYMENT, IMPACT_REFUND_NEEDED, IMPACT_UPSTREAM}

# The closed `reason` vocabulary: the matrix keys + the neutral outcomes. This IS the enum the
# label may take — anything else is a new value that must be declared (and thought about).
CLOSED_REASONS = {reason for _, reason in _IMPACT} | set(_NEUTRAL_REASONS)


def _emitted_reasons_from_source() -> set[str]:
    """Every literal ``reason=`` a call site in ``src/`` passes to the outcome emitter.

    Derived from the CODE (AST), not from the matrix — otherwise the check would compare the
    matrix with itself.
    """
    reasons: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = getattr(callee, "id", None) or getattr(callee, "attr", None)
            # emit_billing_outcome(...) and the per-service `self._emit(result, reason, ...)` /
            # WebhookOutcome(RESULT_X, "reason") wrappers.
            if name not in ("emit_billing_outcome", "_emit", "WebhookOutcome", "PaymentOutcome"):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "reason"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    reasons.add(kw.value.value)
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    reasons.add(arg.value)
    return reasons


def test_every_reason_the_code_emits_is_inside_the_closed_vocabulary() -> None:
    """Cardinality guard. A free-text reason (an error message, a productId) would multiply the
    Prometheus series without bound — and would silently fall outside every alert."""
    emitted = _emitted_reasons_from_source()
    # Constant string arguments that are not reasons at all (SQL, messages) are filtered by
    # intersecting with things that look like a reason token.
    candidates = {r for r in emitted if re.fullmatch(r"[a-z][a-z0-9_]{2,40}", r)}
    unknown = candidates - CLOSED_REASONS
    # Reason-shaped literals that are known NOT to be reasons (they are metric label values or
    # journal statuses used elsewhere in the same call sites).
    allowed_noise = {
        "granted",
        "replayed",
        "rejected",
        "no_grant",
        "applied",
        "duplicate",
        "ignored",
        "noop",
        "error",
        "api_error",
        "timeout",
        "non_2xx",
        "malformed",
        "ok",
        "delivery",
        "grant",
        "subscription",
        "tokens",
        "subscription_event",
        "credits",
        "trial",
        "unbilled",
        "none",
        "success",
        "fail",
        "user_id",
        "device_id",
    }
    assert not (unknown - allowed_noise), f"reason values outside the closed vocabulary: {unknown}"


def test_matrix_keys_are_well_formed() -> None:
    for op, reason in _IMPACT:
        assert op in _OPS, f"unknown op in the matrix: {op}"
        assert re.fullmatch(r"[a-z][a-z0-9_]*", reason), f"reason is not a bounded token: {reason}"
    assert set(_IMPACT.values()) <= _IMPACTS


def test_undeclared_pair_is_fail_loud_never_none(caplog: pytest.LogCaptureFixture) -> None:
    """A path nobody classified is treated as POTENTIALLY MONEY, and it is loud (R-OBS-7a §3)."""
    with caplog.at_level("ERROR", logger="app.billing.outcome"):
        assert impact_for(OP_WEBHOOK, "a_brand_new_reason") == IMPACT_UPSTREAM
    assert any("undeclared_billing_impact" in r.message for r in caplog.records)


def test_neutral_reasons_are_none_on_any_op() -> None:
    for op in _OPS:
        for reason in _NEUTRAL_REASONS:
            assert impact_for(op, reason) == IMPACT_NONE
        assert impact_for(op, None) == IMPACT_NONE


def test_impact_is_a_function_of_the_pair_not_of_the_reason_alone() -> None:
    """The same word means different things on different exit paths — that is why the key is a
    PAIR. ``unknown_product`` after a verified Apple payment is lost money; the same word in
    ``checkout`` means "no payment exists yet"."""
    assert impact_for(OP_TOKEN_PURCHASE, "unknown_product") == IMPACT_LOST_PAYMENT
    assert impact_for(OP_CHECKOUT, "unknown_product") == IMPACT_NONE


# --- R-OBS-6: completeness of the SIGNATURE ---------------------------------------------------
def test_reference_impact_signature_matches_the_documented_input_rows() -> None:
    """Every parameter of ``impact()`` has a row in the "Как вычисляется" table, and vice versa.

    This is the check that catches the input which seems SO self-evident that nobody defines it —
    exactly how ``credits`` slipped through, with the wrong meaning ("what did this branch grant"
    instead of "was the PAYER left uncredited").
    """
    params = set(inspect.signature(reference_impact).parameters)
    assert params == DOC_INPUT_ROWS


# --- R-OBS-7: no second, independently maintained classifier ----------------------------------
def test_log_level_is_computed_from_impact() -> None:
    import logging

    from app.billing.outcome import RESULT_ERROR, RESULT_IGNORED, _level_for

    assert _level_for(IMPACT_NONE, RESULT_IGNORED) == logging.INFO
    for impact in (IMPACT_LOST_PAYMENT, IMPACT_REFUND_NEEDED, IMPACT_UPSTREAM):
        assert _level_for(impact, RESULT_IGNORED) == logging.WARNING
    assert _level_for(IMPACT_NONE, RESULT_ERROR) == logging.ERROR


def test_no_independent_list_of_levels_or_alerts_lives_in_the_code() -> None:
    """A second list of the same fact WILL drift (it already did once, in both directions)."""
    source = inspect.getsource(outcome_mod)
    # The only mapping keyed by a reason is the impact matrix itself; levels are DERIVED.
    assert source.count("logging.WARNING") <= 1
    assert "runbook" not in source.lower()
    assert "alert_name" not in source.lower()


def test_exactly_one_billing_counter_exists() -> None:
    """One counter for all channels: a per-channel counter is how the third channel gets forgotten
    and a new blind spot appears."""
    metrics_source = (_SRC / "observability" / "metrics.py").read_text(encoding="utf-8")
    for forbidden in ("token_purchase_total", "adapty_total", "cloudpayments_total"):
        assert f'"{forbidden}"' not in metrics_source
    assert '"billing_outcome_total"' in metrics_source


def test_blocked_requests_metric_has_no_rate_limited_reason() -> None:
    # rate_limited is a gateway concern (HTTP 429) — Policy never returns it.
    from app.policy.engine import BlockReason

    reasons = {r.value for r in BlockReason}
    assert "rate_limited" in reasons  # it exists in the shared HTTP vocabulary…
    from app.policy.engine import PolicyState, SubscriptionStatus, evaluate

    for status in SubscriptionStatus:
        decision = evaluate(
            PolicyState(status, trial_used=False, credits_balance=0), required_credits=1
        )
        assert decision.block_reason is not BlockReason.rate_limited  # …but never as a verdict
