"""Every ``DomainRegistry`` field is WIRED — proven by its observable EFFECT.

"Declared ≠ wired." A field the core never reads fails SILENTLY and looks exactly like a working
one: the domain registers a ``PolicyGate``, the team believes access control is on, ``evaluate()``
keeps answering ``allowed``, no error anywhere. Same shape as a Prometheus gauge that is never
``.set()`` — except here the dead thing is authorization.

Ф10 acceptance ("the core diff is empty") cannot catch it either: an empty diff looks identical
whether the field is wired or not.

Hence: one test per field asserting a BEHAVIOUR CHANGE, plus a META-TEST that the number of fields
covered equals ``len(dataclasses.fields(DomainRegistry))`` — a 13th field cannot be added without
either wiring it or turning this file red.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import CoreSettings, get_settings
from app.extensions.registry import BodyLimitRule, DomainRegistry
from app.generation.contract import (
    GenerationResult,
    GenerationStatus,
    GenerationUsage,
    ProviderHealth,
)
from app.policy.engine import BlockReason, Decision, PolicyState
from tests.conftest import auth_headers, seed_user, truncate_all

# Every field of DomainRegistry must appear here, mapped to the test that proves its EFFECT.
COVERED_FIELDS: dict[str, str] = {
    "routers": "test_routers_are_served",
    "openapi_tags": "test_openapi_tags_and_description_reach_the_schema",
    "api_description": "test_openapi_tags_and_description_reach_the_schema",
    "body_limit_rules": "test_body_limit_rule_raises_the_limit_only_on_its_own_path",
    "security_headers_exempt_prefixes": "test_security_headers_exempt_prefix_is_honoured",
    "policy_gates": "test_domain_policy_gate_actually_blocks_a_generation",
    "generation_provider": "test_generation_provider_from_the_registry_is_the_one_called",
    "pricing_policy": "test_pricing_policy_from_the_registry_sets_the_price",
    "settings_cls": "test_domain_settings_are_readable_through_get_settings",
    "metrics_module": "test_metrics_module_is_imported_on_startup",
    "truncate_tables": "test_truncate_tables_resets_a_domain_table",
    "on_startup": "test_startup_and_shutdown_hooks_run",
    "on_shutdown": "test_startup_and_shutdown_hooks_run",
}

# Modules that imported `load_registry` BY NAME at import time — patching only the loader module
# would leave them reading the real registry.
_REGISTRY_CONSUMERS = (
    "app.main",
    "app.config",
    "app.policy.loader",
    "app.generation.service",
    "app.generation.registry",
    "app.extensions.loader",
)


def use_registry(monkeypatch: pytest.MonkeyPatch, registry: DomainRegistry) -> None:
    import importlib

    for module_name in _REGISTRY_CONSUMERS:
        module = importlib.import_module(module_name)
        if hasattr(module, "load_registry"):
            monkeypatch.setattr(module, "load_registry", lambda r=registry: r)  # type: ignore[misc]
    get_settings.cache_clear()


@pytest.fixture
def app_with(
    sessionmaker_: async_sessionmaker[AsyncSession],
    allow_limits: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Build the app on an arbitrary registry, wired to the container DB."""

    def _build(registry: DomainRegistry) -> Any:
        from app import deps
        from app.main import create_app

        use_registry(monkeypatch, registry)

        async def _override_db() -> AsyncIterator[AsyncSession]:
            async with sessionmaker_() as s:
                try:
                    yield s
                    await s.commit()
                except Exception:
                    await s.rollback()
                    raise

        application = create_app()
        application.dependency_overrides[deps.get_db] = _override_db
        return application

    yield _build
    get_settings.cache_clear()


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- routers / tags / description --------------------------------------------------------------
async def test_routers_are_served(app_with: Any) -> None:
    router = APIRouter(prefix="/v1/demo")

    @router.get("/ping")
    async def _ping() -> dict[str, str]:
        return {"pong": "yes"}

    app = app_with(DomainRegistry(routers=(router,)))
    async with _client(app) as ac:
        response = await ac.get("/v1/demo/ping")
    assert response.status_code == 200 and response.json() == {"pong": "yes"}


async def test_openapi_tags_and_description_reach_the_schema(app_with: Any) -> None:
    app = app_with(
        DomainRegistry(
            openapi_tags=({"name": "DemoTag", "description": "d"},),
            api_description="\n\nDOMAIN-SECTION-MARKER",
        )
    )
    schema = app.openapi()
    assert any(tag["name"] == "DemoTag" for tag in schema["tags"])
    assert "DOMAIN-SECTION-MARKER" in schema["info"]["description"]


# --- middleware rules ---------------------------------------------------------------------------
async def test_body_limit_rule_raises_the_limit_only_on_its_own_path(app_with: Any) -> None:
    router = APIRouter(prefix="/v1/demo")

    @router.post("/upload")
    async def _upload() -> dict[str, str]:
        return {"ok": "1"}

    @router.post("/neighbour")
    async def _neighbour() -> dict[str, str]:
        return {"ok": "1"}

    app = app_with(
        DomainRegistry(
            routers=(router,),
            body_limit_rules=(BodyLimitRule(match="/v1/demo/upload", limit=2 * 1024 * 1024),),
        )
    )
    big = b"x" * (600 * 1024)  # over the general 512 KB limit
    async with _client(app) as ac:
        raised = await ac.post("/v1/demo/upload", content=big)
        neighbour = await ac.post("/v1/demo/neighbour", content=big)

    assert raised.status_code != 413  # the rule was READ — the raise applies
    assert neighbour.status_code == 413  # …and does NOT leak onto the neighbouring route
    assert neighbour.json()["error"]["code"] == "payload_too_large"


async def test_security_headers_exempt_prefix_is_honoured(app_with: Any) -> None:
    router = APIRouter()

    @router.get("/v1/preview/{page}")
    async def _preview(page: str) -> dict[str, str]:
        return {"page": page}

    @router.get("/v1/regular")
    async def _regular() -> dict[str, str]:
        return {"ok": "1"}

    app = app_with(
        DomainRegistry(routers=(router,), security_headers_exempt_prefixes=("/v1/preview",))
    )
    async with _client(app) as ac:
        exempt = await ac.get("/v1/preview/p1")
        regular = await ac.get("/v1/regular")

    assert "X-Frame-Options" not in exempt.headers  # serves its own header set
    assert regular.headers["X-Frame-Options"] == "DENY"  # the neighbour keeps the core defaults


# --- business logic ------------------------------------------------------------------------------
class _AlwaysBlockGate:
    """A domain gate that blocks everything. Declares the impact of its reason (R-OBS-5)."""

    block_impacts = {"policy_denied": "none"}

    def check(self, state: PolicyState, ctx: Any) -> Decision | None:
        return Decision.block(BlockReason.policy_denied)


async def test_domain_policy_gate_actually_blocks_a_generation(
    app_with: Any, session: AsyncSession, fake_provider: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE most important wiring test: an unread ``policy_gates`` is access control that is OFF
    while everyone believes it is ON."""
    from app import deps
    from app.domain.routers import generate as generate_router

    user_id = await seed_user(session, subscription="active", balance=100)

    monkeypatch.setattr(deps, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(generate_router, "get_provider", lambda: fake_provider)

    app = app_with(
        DomainRegistry(routers=(generate_router.router,), policy_gates=(_AlwaysBlockGate(),))
    )
    async with _client(app) as ac:
        response = await ac.post("/v1/generate", json={"params": {}}, headers=auth_headers(user_id))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == "policy_denied"
    assert fake_provider.calls == []  # the provider was never called
    # …and no row was created: a block means the provider was NOT called.
    count = await session.scalar(
        text("SELECT count(*) FROM generations WHERE user_id = :u"), {"u": str(user_id)}
    )
    assert int(count or 0) == 0


class _RegistryProvider:
    name = "registry-provider"
    kind = "demo-kind"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, req: Any) -> GenerationResult:
        self.calls += 1
        return GenerationResult(
            status=GenerationStatus.succeeded,
            output={"from": "registry"},
            usage=GenerationUsage(model="m", units=1),
            stop_reason="end",
        )

    async def poll(self, provider_ref: str) -> GenerationResult:
        raise NotImplementedError

    async def healthcheck(self) -> ProviderHealth:
        return ProviderHealth(healthy=True)


class _RegistryPricing:
    def quote(self, *, kind: str, model: str | None, params: dict[str, Any]) -> int:
        return 7

    def charge(self, *, kind: str, model: str | None, usage: GenerationUsage) -> int:
        return 7


async def test_generation_provider_from_the_registry_is_the_one_called(
    app_with: Any, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.generation import registry as registry_mod

    provider = _RegistryProvider()
    monkeypatch.setenv("GENERATION_PROVIDER", provider.name)
    registry_mod.get_provider.cache_clear()
    registry_mod.get_pricing.cache_clear()

    user_id = await seed_user(session, subscription="active", balance=100)
    from app.domain.routers import generate as generate_router

    app = app_with(DomainRegistry(routers=(generate_router.router,), generation_provider=provider))
    try:
        async with _client(app) as ac:
            response = await ac.post(
                "/v1/generate", json={"params": {}}, headers=auth_headers(user_id)
            )
        assert response.status_code == 200, response.text
        assert response.json()["output"] == {"from": "registry"}
        assert provider.calls == 1
        row = (
            await session.execute(
                text("SELECT provider, kind FROM generations WHERE user_id = :u"),
                {"u": str(user_id)},
            )
        ).first()
        assert row is not None and row[0] == "registry-provider" and row[1] == "demo-kind"
    finally:
        registry_mod.get_provider.cache_clear()
        registry_mod.get_pricing.cache_clear()


async def test_pricing_policy_from_the_registry_sets_the_price(
    app_with: Any, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.generation import registry as registry_mod

    provider = _RegistryProvider()
    monkeypatch.setenv("GENERATION_PROVIDER", provider.name)
    registry_mod.get_provider.cache_clear()
    registry_mod.get_pricing.cache_clear()

    user_id = await seed_user(session, subscription="active", balance=100)
    from app.domain.routers import generate as generate_router

    app = app_with(
        DomainRegistry(
            routers=(generate_router.router,),
            generation_provider=provider,
            pricing_policy=_RegistryPricing(),
        )
    )
    try:
        async with _client(app) as ac:
            response = await ac.post(
                "/v1/generate", json={"params": {}}, headers=auth_headers(user_id)
            )
        assert response.status_code == 200, response.text
        assert response.json()["creditsCharged"] == 7  # not the built-in flat price of 1
        assert response.json()["newBalance"] == 93
    finally:
        registry_mod.get_provider.cache_clear()
        registry_mod.get_pricing.cache_clear()


# --- configuration --------------------------------------------------------------------------------
async def test_domain_settings_are_readable_through_get_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DomainSettings(CoreSettings):
        demo_flag: str = "off"

    monkeypatch.setenv("DEMO_FLAG", "on")
    use_registry(monkeypatch, DomainRegistry(settings_cls=DomainSettings))
    try:
        settings = get_settings()
        assert isinstance(settings, DomainSettings)
        assert settings.demo_flag == "on"  # the domain's env var actually reaches the app
    finally:
        get_settings.cache_clear()


# --- observability ---
async def test_metrics_module_is_imported_on_startup(app_with: Any) -> None:
    """The domain's metric module is imported at startup — that import is what REGISTERS the
    series in the process-global Prometheus registry."""
    from prometheus_client import REGISTRY

    app = app_with(DomainRegistry(metrics_module="tests.support.domain_metrics"))
    async with app.router.lifespan_context(app):
        pass
    assert REGISTRY.get_sample_value("demo_domain_metric_total") is not None


# --- lifecycle ---
async def test_startup_and_shutdown_hooks_run(app_with: Any) -> None:
    calls: list[str] = []

    async def _startup() -> None:
        calls.append("startup")

    async def _shutdown() -> None:
        calls.append("shutdown")

    app = app_with(DomainRegistry(on_startup=(_startup,), on_shutdown=(_shutdown,)))
    async with app.router.lifespan_context(app):
        assert calls == ["startup"]
    assert calls == ["startup", "shutdown"]


# --- tests-only field ---
async def test_truncate_tables_resets_a_domain_table(engine: Any) -> None:
    """``truncate_tables`` is the seam that keeps DOMAIN state from leaking across tests.

    The effect is falsifiable: with the table declared, the row is gone; with an empty registry it
    survives (which is exactly the leak the field prevents).
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS demo_domain_rows (id int)"))
        await conn.execute(text("TRUNCATE demo_domain_rows"))
        await conn.execute(text("INSERT INTO demo_domain_rows (id) VALUES (1)"))

    await truncate_all(engine, DomainRegistry())  # not declared → the row LEAKS
    async with engine.connect() as conn:
        leaked = await conn.scalar(text("SELECT count(*) FROM demo_domain_rows"))
    assert int(leaked or 0) == 1

    await truncate_all(engine, DomainRegistry(truncate_tables=("demo_domain_rows",)))
    async with engine.connect() as conn:
        cleared = await conn.scalar(text("SELECT count(*) FROM demo_domain_rows"))
    assert int(cleared or 0) == 0

    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE demo_domain_rows"))


# --- THE META-TEST ---
def test_every_registry_field_has_an_effect_test() -> None:
    """A 13th field cannot be added without wiring it — or this goes red.

    Same device as the ``impact()`` signature check: the completeness of the CONTRACT is asserted
    against the code, not against a list somebody must remember to update.
    """
    declared = {f.name for f in dataclasses.fields(DomainRegistry)}
    covered = set(COVERED_FIELDS)
    assert declared == covered, (
        f"DomainRegistry fields without an EFFECT test: {sorted(declared - covered)}; "
        f"stale entries: {sorted(covered - declared)}"
    )
    # …and every named test really exists in this module.
    for field_name, test_name in COVERED_FIELDS.items():
        assert test_name in globals(), f"{field_name} points at a missing test {test_name}"
