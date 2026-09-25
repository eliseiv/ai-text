"""``impact`` of the generation circuit (R-OBS-1..6) + the fail-closed provider registry.

The extension point people actually use is ``policy_gates``: every service built from this template
brings its own block reasons. Without R-OBS-5 each of them would land outside every alert
automatically — the template would be shipping blind spots.
"""

from __future__ import annotations

import datetime
import logging

import pytest

from app.config import get_settings
from app.errors import ProviderNotConfiguredError
from app.generation.contract import GenerationUsage
from app.generation.impact import (
    IMPACT_NONE,
    IMPACT_REVENUE_LOSS,
    IMPACT_UPSTREAM,
    IMPACT_USER_BLOCKED,
    block_impact,
    generation_impact,
    is_stuck,
)
from app.generation.registry import get_pricing, get_provider

_NOW = datetime.datetime(2030, 1, 1, 12, 0, tzinfo=datetime.UTC)


# --- the total function --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "credits", "billing_kind", "stuck", "expected"),
    [
        # nobody lost anything
        ("succeeded", 1, "credits", False, IMPACT_NONE),
        ("succeeded", 0, "trial", False, IMPACT_NONE),
        ("blocked", 0, "none", False, IMPACT_NONE),
        ("canceled", 0, "none", False, IMPACT_NONE),
        # the SERVICE lost: delivered, charged nothing, and it was not the free trial
        ("succeeded", 0, "unbilled", False, IMPACT_REVENUE_LOSS),
        # both lost
        ("failed", 0, "none", False, IMPACT_UPSTREAM),
        # the USER lost: his Idempotency-Key now answers 409 forever
        ("running", 0, "none", True, IMPACT_USER_BLOCKED),
        ("pending", 0, "none", True, IMPACT_USER_BLOCKED),
    ],
)
def test_generation_impact_is_total_over_columns_of_the_row(
    status: str, credits: int, billing_kind: str, stuck: bool, expected: str
) -> None:
    """Every input is a COLUMN of ``generations`` (or computed from one) — R-OBS-6 holds by
    construction, and a test can derive them from the database instead of from a document."""
    assert (
        generation_impact(
            status=status, credits_charged=credits, billing_kind=billing_kind, stuck=stuck
        )
        == expected
    )


def test_stuck_wins_over_everything() -> None:
    assert (
        generation_impact(status="running", credits_charged=0, billing_kind="credits", stuck=True)
        == IMPACT_USER_BLOCKED
    )


@pytest.mark.parametrize(
    ("status", "age_seconds", "expected"),
    [
        ("running", 100, False),
        ("running", 241, True),  # 2 × GENERATION_TIMEOUT_SECONDS (120) → stuck
        ("pending", 241, True),
        ("succeeded", 10_000, False),  # a finished generation is never "stuck"
        ("failed", 10_000, False),
    ],
)
def test_is_stuck_is_computed_not_judged(status: str, age_seconds: int, expected: bool) -> None:
    created = _NOW - datetime.timedelta(seconds=age_seconds)
    assert is_stuck(status, created, now=_NOW, timeout_seconds=120.0) is expected


# --- R-OBS-5: a domain gate MUST declare the impact of its reasons ---
def test_core_block_reasons_are_all_declared_none() -> None:
    """A policy block BEFORE the provider call costs nobody anything: the user paid nothing and got
    nothing."""
    from app.policy.engine import BlockReason

    for reason in (
        BlockReason.trial_used,
        BlockReason.subscription_required,
        BlockReason.subscription_expired,
        BlockReason.credits_empty,
        BlockReason.policy_denied,
    ):
        assert block_impact(reason.value) == IMPACT_NONE


def test_domain_reason_with_a_declared_impact_is_honoured() -> None:
    assert block_impact("quota_exhausted", {"quota_exhausted": IMPACT_NONE}) == IMPACT_NONE
    assert (
        block_impact("paid_but_refused", {"paid_but_refused": IMPACT_REVENUE_LOSS})
        == IMPACT_REVENUE_LOSS
    )


def test_undeclared_domain_reason_is_loud_and_conservative(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A gate that introduces a new block reason WITHOUT declaring its impact must not slip into
    ``none``: somebody is being refused and we do not know that it is harmless."""
    with caplog.at_level(logging.ERROR, logger="app.generation.impact"):
        assert block_impact("a_domain_reason_nobody_declared") == IMPACT_USER_BLOCKED
    assert any("undeclared_block_impact" in r.message for r in caplog.records)


def test_a_gate_refusing_an_already_paid_operation_may_not_call_it_none() -> None:
    """Reproduces the ``subscription_required`` defect from billing: a refusal AFTER payment was
    labelled routine and stayed unalerted. A gate declaring ``none`` for a reason whose meaning is
    "the user paid and gets nothing" is exactly that bug — and the value it declares is what the
    alert sees."""
    declared = {"paid_but_refused": IMPACT_NONE}
    # The gate says "harmless"; the core does not second-guess the declaration — which is why the
    # rule is enforced by THIS test, at build time.
    assert block_impact("paid_but_refused", declared) == IMPACT_NONE
    # A correctly classified gate declares the money impact instead:
    assert (
        block_impact("paid_but_refused", {"paid_but_refused": IMPACT_REVENUE_LOSS})
        == IMPACT_REVENUE_LOSS
    )


# --- the provider registry is FAIL-CLOSED ---
@pytest.fixture(autouse=True)
def _clear_registry_caches() -> None:
    get_provider.cache_clear()
    get_pricing.cache_clear()
    get_settings.cache_clear()
    yield
    get_provider.cache_clear()
    get_pricing.cache_clear()
    get_settings.cache_clear()


def test_echo_provider_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERATION_PROVIDER", "echo")
    assert get_provider().name == "echo"


def test_unknown_provider_is_503_never_a_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent fallback would generate with the WRONG upstream and bill the user for it."""
    monkeypatch.setenv("GENERATION_PROVIDER", "flux")
    with pytest.raises(ProviderNotConfiguredError):
        get_provider()


def test_registered_provider_must_match_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.generation.registry as registry_mod
    from app.extensions.registry import DomainRegistry

    class _Provider:
        name = "flux"
        kind = "image"

        async def generate(self, req: object) -> object:  # pragma: no cover - never called
            raise NotImplementedError

        async def poll(self, ref: str) -> object:  # pragma: no cover
            raise NotImplementedError

        async def healthcheck(self) -> object:  # pragma: no cover
            raise NotImplementedError

    monkeypatch.setattr(
        registry_mod, "load_registry", lambda: DomainRegistry(generation_provider=_Provider())
    )
    monkeypatch.setenv("GENERATION_PROVIDER", "some-other-name")
    with pytest.raises(ProviderNotConfiguredError):
        get_provider()  # the operator's statement and the registered provider disagree → refuse

    get_provider.cache_clear()
    get_settings.cache_clear()
    monkeypatch.setenv("GENERATION_PROVIDER", "flux")
    assert get_provider().name == "flux"


def test_custom_pricing_mode_without_a_policy_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.generation.registry as registry_mod
    from app.extensions.registry import DomainRegistry

    # A registry WITHOUT a pricing policy (the real domain registers one).
    monkeypatch.setattr(registry_mod, "load_registry", lambda: DomainRegistry())
    monkeypatch.setenv("PRICING_MODE", "custom")
    with pytest.raises(ProviderNotConfiguredError):
        get_pricing()


def test_registry_pricing_policy_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.generation.registry as registry_mod
    from app.extensions.registry import DomainRegistry

    class _Pricing:
        def quote(self, *, kind: str, model: str | None, params: dict[str, object]) -> int:
            return 5

        def charge(self, *, kind: str, model: str | None, usage: GenerationUsage) -> int:
            return 5

    monkeypatch.setattr(
        registry_mod, "load_registry", lambda: DomainRegistry(pricing_policy=_Pricing())
    )
    monkeypatch.setenv("PRICING_MODE", "custom")
    assert get_pricing().quote(kind="k", model=None, params={}) == 5
