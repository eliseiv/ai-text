"""``DomainRegistry`` — the plug-in mechanism. Empty registry ⇒ the app still runs."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter

from app.config import CoreSettings, get_settings
from app.extensions.loader import load_registry
from app.extensions.registry import EMPTY_REGISTRY, BodyLimitRule, DomainRegistry
from app.generation.contract import GenerationUsage
from app.policy.engine import BillingKind, BlockReason, Decision, PolicyState, SubscriptionStatus
from app.policy.loader import apply_gates


def test_empty_registry_is_fully_functional() -> None:
    registry = DomainRegistry()
    assert registry.routers == ()
    assert registry.policy_gates == ()
    assert registry.generation_provider is None
    assert registry.pricing_policy is None
    assert registry.settings_cls is None
    assert registry.truncate_tables == ()
    assert DomainRegistry() == EMPTY_REGISTRY


def test_app_starts_on_an_empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "load_registry", lambda: DomainRegistry())
    application = main_mod.create_app()
    paths = set(application.openapi()["paths"])
    assert "/v1/auth/register" in paths
    assert "/v1/generate" not in paths  # the sample route is a DOMAIN contribution


def test_registry_router_tag_and_provider_are_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    router = APIRouter(prefix="/v1/demo", tags=["Demo"])

    @router.get("/ping")
    async def _ping() -> dict[str, str]:
        return {"pong": "1"}

    registry = DomainRegistry(
        routers=(router,),
        openapi_tags=({"name": "Demo", "description": "domain tag"},),
        api_description="\n\nDomain section.",
        body_limit_rules=(BodyLimitRule(match="/v1/demo/upload", limit=1024),),
    )
    monkeypatch.setattr(main_mod, "load_registry", lambda: registry)
    application = main_mod.create_app()

    schema = application.openapi()
    assert "/v1/demo/ping" in schema["paths"]
    assert any(t["name"] == "Demo" for t in schema["tags"])
    assert "Domain section." in schema["info"]["description"]


def test_domain_settings_subclass_is_instantiated(monkeypatch: pytest.MonkeyPatch) -> None:
    class DomainSettings(CoreSettings):
        my_domain_flag: str = "on"

    import app.config as config_mod

    monkeypatch.setattr(
        config_mod, "load_registry", lambda: DomainRegistry(settings_cls=DomainSettings)
    )
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert isinstance(settings, DomainSettings)
        assert settings.my_domain_flag == "on"
    finally:
        get_settings.cache_clear()


def test_core_settings_ignore_unknown_domain_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # extra="ignore" is REQUIRED for the DomainSettings inheritance.
    monkeypatch.setenv("SOME_DOMAIN_ONLY_KEY", "value")
    assert CoreSettings().service_name  # does not explode


def test_policy_gates_may_only_tighten_the_core_decision() -> None:
    class DenyGate:
        block_impacts = {"domain_denied": "none"}

        def check(self, state: PolicyState, ctx: Any) -> Decision | None:
            return Decision.block(BlockReason.policy_denied)

    class AllowEverythingGate:
        def check(self, state: PolicyState, ctx: Any) -> Decision | None:
            # A domain must NOT be able to hand out access the core denied — that is billing bypass.
            return Decision(allowed=True)

    state = PolicyState(
        subscription_status=SubscriptionStatus.active, trial_used=True, credits_balance=10
    )

    import app.policy.loader as loader_mod

    core_block = Decision.block(BlockReason.credits_empty)
    core_allow = Decision.allow(BillingKind.credits)

    loader_mod.load_registry = lambda: DomainRegistry(policy_gates=(AllowEverythingGate(),))  # type: ignore[assignment]
    assert apply_gates(core_block, state) is core_block  # the gate's "allow" is ignored

    loader_mod.load_registry = lambda: DomainRegistry(policy_gates=(DenyGate(),))  # type: ignore[assignment]
    tightened = apply_gates(core_allow, state)
    assert tightened.allowed is False
    assert tightened.block_reason is BlockReason.policy_denied

    loader_mod.load_registry = load_registry  # type: ignore[assignment]


def test_body_limit_rule_matches_precisely() -> None:
    rule = BodyLimitRule(match="/v1/workspaces/*/files", limit=8 * 1024 * 1024)
    assert rule.matches("/v1/workspaces/abc/files", "POST") is True
    assert rule.matches("/v1/workspaces//files", "POST") is False  # `*` needs ≥ 1 char
    assert rule.matches("/v1/workspaces/abc", "POST") is False  # CRUD must NOT get the raise
    assert rule.matches("/v1/workspaces/abc/files/xyz", "POST") is False  # nor the delete route
    assert rule.matches("/v1/workspaces/abc/files", "GET") is False  # a GET carries no body


def test_exact_match_rule() -> None:
    rule = BodyLimitRule(match="/v1/upload", limit=10, methods=("POST",))
    assert rule.matches("/v1/upload", "POST") is True
    assert rule.matches("/v1/upload", "PUT") is False
    assert rule.matches("/v1/upload/x", "POST") is False


def test_provider_and_pricing_can_be_supplied_as_data() -> None:
    class Pricing:
        def quote(self, *, kind: str, model: str | None, params: dict[str, Any]) -> int:
            return 42

        def charge(self, *, kind: str, model: str | None, usage: GenerationUsage) -> int:
            return 42

    registry = DomainRegistry(pricing_policy=Pricing())
    assert registry.pricing_policy is not None
    assert registry.pricing_policy.quote(kind="k", model=None, params={}) == 42


def test_the_template_registry_is_the_one_the_loader_returns() -> None:
    from app.domain import REGISTRY

    assert load_registry() is REGISTRY
    # The sources domain: its own routers (+ the dev-only core harness route), settings, pricing
    # and tables. A silently empty registry (ImportError inside the domain) would fail here.
    from app.domain.config import DomainSettings
    from app.domain.pricing import DomainPricing

    paths = {route.path for router in REGISTRY.routers for route in router.routes}
    assert {"/v1/topics", "/v1/topics/{topic_id}/messages", "/v1/limits"} <= paths
    assert REGISTRY.settings_cls is DomainSettings
    assert isinstance(REGISTRY.pricing_policy, DomainPricing)
    assert "chunks" in REGISTRY.truncate_tables
    assert REGISTRY.generation_provider is None  # generation runs in the worker, not /v1/generate
