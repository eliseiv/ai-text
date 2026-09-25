"""Test scaffolding: forced env, ephemeral RSA, real PostgreSQL (testcontainers), fakes.

Hermetic by construction:

* PostgreSQL is REAL (testcontainers) — half of the money invariants live in CHECK/UNIQUE
  constraints and a mock cannot check them;
* every outgoing HTTP boundary (broadapps verify/checkout, Apple JWKS) is faked (``respx`` /
  injected fakes) — no test performs a real network call;
* Redis is absent: every limiter fails open on ``RedisError`` and the ``client`` fixture patches
  them to a deterministic "allow" anyway. Rate-limit behaviour is exercised by forcing a limiter
  to deny.

The env is FORCED (not ``setdefault``) BEFORE ``app.config`` is imported: ``get_settings()`` is
``lru_cache``d, so the first read wins for the whole process, and a stray ``.env`` in the repo root
would otherwise decide the behaviour of the suite.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

# --------------------------------------------------------------------------------------------
# FORCED ENVIRONMENT — must precede any `app.*` import (get_settings is lru_cache'd).
# --------------------------------------------------------------------------------------------
os.environ["SERVICE_NAME"] = "service-template-tests"
os.environ["SERVICE_TITLE"] = ""
os.environ["SERVICE_VERSION"] = "9.9.9"
os.environ["ENVIRONMENT"] = "dev"
os.environ["DOCS_ENABLED"] = "true"
os.environ["LOG_LEVEL"] = "INFO"

# Redis is never started: the limiters fail open, and the client fixture patches them.
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"

# --- Products: THE catalogue. Deliberately includes an Apple-only product so a CloudPayments
# callback naming it exercises `product_not_in_channel` (the hole the source had).
PRODUCT_SUB = "sub.monthly"
PRODUCT_SUB_CREDITS = 1000
PRODUCT_SUB_APPLE_ONLY = "sub.apple_only"
PRODUCT_TOKENS = "tokens.100"
PRODUCT_TOKENS_CREDITS = 100
PRODUCT_TOKENS_APPLE_ONLY = "tokens.apple"
# Sold ONLY through the RU channel — a StoreKit purchase naming them must hit
# `product_not_in_channel` (the symmetric hole to a CloudPayments callback naming an Apple id).
PRODUCT_SUB_RU_ONLY = "sub.ru_only"
PRODUCT_TOKENS_RU_ONLY = "tokens.ru_only"
_PRODUCTS = {
    PRODUCT_SUB: {
        "kind": "subscription",
        "credits": PRODUCT_SUB_CREDITS,
        "channels": ["apple_storekit", "adapty", "cloudpayments"],
        "title": "Monthly",
        "period": "P1M",
    },
    PRODUCT_SUB_APPLE_ONLY: {
        "kind": "subscription",
        "credits": 500,
        "channels": ["apple_storekit"],
    },
    PRODUCT_TOKENS: {
        "kind": "tokens",
        "credits": PRODUCT_TOKENS_CREDITS,
        "channels": ["apple_storekit", "cloudpayments"],
    },
    PRODUCT_TOKENS_APPLE_ONLY: {
        "kind": "tokens",
        "credits": 50,
        "channels": ["apple_storekit"],
    },
    PRODUCT_SUB_RU_ONLY: {
        "kind": "subscription",
        "credits": 300,
        "channels": ["cloudpayments"],
        "period": "P1M",
    },
    PRODUCT_TOKENS_RU_ONLY: {
        "kind": "tokens",
        "credits": 30,
        "channels": ["cloudpayments"],
    },
}
os.environ["PRODUCTS"] = json.dumps(_PRODUCTS)
os.environ["SUBSCRIPTION_CREDITS_PER_PERIOD"] = "1000"

# --- StoreKit: PROD posture (test-mode OFF) so the crypto unit tests are honest (HS256 → 422).
# Integration flows inject FakeStoreKitVerifier instead of weakening the real verifier.
os.environ["APPSTORE_BUNDLE_ID"] = "com.example.app"
os.environ["APPSTORE_ENVIRONMENT"] = "Production"
os.environ["APPSTORE_ROOT_CERT_DIR"] = ""
os.environ["STOREKIT_TEST_MODE"] = "false"
os.environ["STOREKIT_TEST_SECRET"] = ""

# --- Apple sign-in: HS256 test seam ON (hermetic), audience explicit.
APPLE_TEST_SECRET = "apple-test-secret"
APPLE_ISSUER = "https://appleid.apple.com"
APPLE_AUDIENCE = "com.example.app"
os.environ["APPLE_OIDC_ISSUER"] = APPLE_ISSUER
os.environ["APPLE_AUDIENCE"] = APPLE_AUDIENCE
os.environ["APPLE_TEST_MODE"] = "true"
os.environ["APPLE_TEST_SECRET"] = APPLE_TEST_SECRET

# --- Billing channels.
ADAPTY_SECRET = "adapty-webhook-secret"
os.environ["ADAPTY_WEBHOOK_SECRET"] = ADAPTY_SECRET
CLOUDPAYMENTS_API_BASE = "https://pay.example.test/api"
os.environ["CLOUDPAYMENTS_API_BASE"] = CLOUDPAYMENTS_API_BASE
os.environ["CLOUDPAYMENTS_APP_ID"] = "app-1"
os.environ["CLOUDPAYMENTS_API_TOKEN"] = "cp-api-token"
os.environ["CLOUDPAYMENTS_WEBHOOK_TOKEN"] = ""
os.environ["CLOUDPAYMENTS_PAID_STATUSES"] = "succeeded"
os.environ["CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS"] = "72"

ADMIN_SECRET = "admin-secret"
os.environ["ADMIN_API_SECRET"] = ADMIN_SECRET
os.environ["ADMIN_API_SECRET_PREV"] = ""

# --- Generation / pricing.
os.environ["GENERATION_PROVIDER"] = "echo"
os.environ["GENERATION_TIMEOUT_SECONDS"] = "5"
os.environ["GENERATION_META_MAX_BYTES"] = "8192"
os.environ["GENERATION_MAX_INFLIGHT_PER_USER"] = "3"
os.environ["PRICING_MODE"] = "flat"
os.environ["PRICING_FLAT_CREDITS"] = "1"
os.environ["PRICING_UNITS"] = "{}"
os.environ["PRICING_TOKEN_WEIGHTS"] = "{}"
os.environ["PRICING_TOKEN_DIVISOR"] = "1000"
os.environ["PRICING_MAX_CREDITS_PER_GENERATION"] = "100"

# --- Gateway.
os.environ["SIZE_LIMIT_BODY"] = str(512 * 1024)
os.environ["TRUSTED_PROXY_IPS"] = ""
os.environ["TRUSTED_PROXY_HOP_COUNT"] = "1"
os.environ["METRICS_SCRAPE_TOKEN"] = ""
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

# --- JWT (embedded RS256 issuer): ephemeral key pair, generated per process.
JWT_ISSUER = "service-template-tests"
JWT_AUDIENCE = "service-template-tests"
JWT_KID = "test-kid"
os.environ["JWT_ISSUER"] = JWT_ISSUER
os.environ["JWT_AUDIENCE"] = JWT_AUDIENCE
os.environ["JWT_KID"] = JWT_KID
os.environ["JWT_JWKS_URL"] = ""
os.environ["JWT_PRIVATE_KEY_PATH"] = ""
os.environ["JWT_PUBLIC_KEY_PATH"] = ""
os.environ["AUTH_ACCESS_TTL_SECONDS"] = "3600"
os.environ["AUTH_REFRESH_TTL_SECONDS"] = "2592000"

import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWT_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
).decode()
JWT_PUBLIC_PEM = (
    _PRIVATE_KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
)
os.environ["JWT_PRIVATE_KEY"] = JWT_PRIVATE_PEM
os.environ["JWT_PUBLIC_KEY"] = JWT_PUBLIC_PEM


def make_jwt(
    user_id: uuid.UUID | str,
    *,
    device_id: str | None = "device-1",
    expired: bool = False,
    extra: dict[str, Any] | None = None,
) -> str:
    """Sign an access token exactly as the embedded issuer would (same iss/aud/alg)."""
    now = datetime.datetime.now(tz=datetime.UTC)
    exp = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=1)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "iat": now,
        "exp": exp,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    if device_id is not None:
        claims["device_id"] = device_id
    if extra:
        claims.update(extra)
    return pyjwt.encode(claims, JWT_PRIVATE_PEM, algorithm="RS256", headers={"kid": JWT_KID})


def auth_headers(user_id: uuid.UUID | str, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(user_id, **kwargs)}"}


def apple_identity_token(
    *,
    subject: str,
    email: str | None = None,
    issuer: str = APPLE_ISSUER,
    audience: str = APPLE_AUDIENCE,
    expired: bool = False,
    algorithm: str = "HS256",
    key: str = APPLE_TEST_SECRET,
    nonce: str | None = None,
) -> str:
    now = datetime.datetime.now(tz=datetime.UTC)
    exp = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=1)
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": exp,
    }
    if email is not None:
        claims["email"] = email
        claims["email_verified"] = True
    if nonce is not None:
        claims["nonce"] = nonce
    return pyjwt.encode(claims, key, algorithm=algorithm)


# --------------------------------------------------------------------------------------------
# PostgreSQL container + migrations
# --------------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        os.environ["DATABASE_URL"] = url
        yield url


def alembic_config(url: str) -> Any:
    """Alembic ``Config`` WITHOUT the ini file — deliberately.

    ``Config("alembic.ini")`` makes ``migrations/env.py`` call ``fileConfig()``, which defaults to
    ``disable_existing_loggers=True`` and silently sets ``disabled=True`` on every logger that
    already exists — including ``app.*``. Every later assertion about a log record (the R-OBS-7
    level checks, the redaction checks) would then pass vacuously, because no record is ever
    emitted at all. A test that cannot fail is worse than no test.
    """
    from alembic.config import Config

    cfg = Config()  # config_file_name stays None => env.py skips fileConfig
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def reenable_loggers() -> None:
    """Undo ``disable_existing_loggers`` should anything ever re-introduce it."""
    import logging

    for logger in logging.root.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            logger.disabled = False


@pytest.fixture(scope="session")
def migrated(pg_url: str) -> Iterator[str]:
    """`alembic upgrade head` once per session against the container."""
    from alembic import command

    command.upgrade(alembic_config(pg_url), "head")
    reenable_loggers()
    yield pg_url


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[Any]:
    # Function-scoped engine: pytest-asyncio gives each test a fresh event loop, and asyncpg
    # connections are loop-bound. Container + migrations stay session-scoped.
    eng = create_async_engine(migrated, future=True, poolclass=NullPool)
    yield eng
    await eng.dispose()


# Core tables + whatever the domain declares. Order matters only
# for readability: TRUNCATE ... CASCADE handles the FKs.
_CORE_TABLES = (
    "audit_logs",
    "payments",
    "generations",
    "ledger_transactions",
    "wallets",
    "subscriptions",
    "user_profiles",
    "auth_refresh_tokens",
    "auth_identities",
    "auth_devices",
    "users",
)


async def truncate_all(engine: Any, registry: Any) -> None:
    """Reset the DB between tests: core tables + whatever the DOMAIN declared.

    ``registry.truncate_tables`` is the wiring point: a domain table missing from it leaks state
    across tests. ``test_domain_registry_wiring.py`` asserts the EFFECT of this line.
    """
    tables = (*_CORE_TABLES, *registry.truncate_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def global_engine_on_container(migrated: str) -> AsyncIterator[None]:
    """Point the PROCESS-GLOBAL engine (``app.db``) at the container too.

    Most code takes its session from the ``get_db`` dependency (overridden below), but a few
    endpoints deliberately do not: ``/metrics`` refreshes the ``generations_inflight`` gauge and
    ``/ready`` probes the DB through ``app.db.get_sessionmaker()``. Unit tests run first and cache
    ``get_settings()`` with the default ``DATABASE_URL``, so without this the global engine would
    point at a database that does not exist — and the gauge test would pass/fail for the wrong
    reason. Nothing here reaches outside the container: it IS the test's own database.
    """
    import app.db as db_mod
    from app.config import get_settings

    get_settings.cache_clear()
    await db_mod.dispose_engine()
    yield
    await db_mod.dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
async def sessionmaker_(
    engine: Any, global_engine_on_container: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from app.extensions.loader import load_registry

    await truncate_all(engine, load_registry())
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def session(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_() as s:
        yield s


# --------------------------------------------------------------------------------------------
# Fakes at the EXTERNAL boundaries only (never of our own code)
# --------------------------------------------------------------------------------------------
class FakeGenerationProvider:
    """Scriptable ``GenerationProvider``: success / ProviderError /
    controlled ``usage``. Never touches the DB or the wallet — like a real provider."""

    def __init__(self, name: str = "echo", kind: str = "echo") -> None:
        self.name = name
        self.kind = kind
        self.calls: list[Any] = []
        self.error: Exception | None = None
        self.delay: float = 0.0
        self.output: dict[str, Any] = {"text": "ok"}
        self.usage_units = 1
        self.usage_model = "fake-model"
        self.unit_kind = "call"
        self.input_tokens = 0
        self.output_tokens = 0
        self.provider_ref: str | None = None
        self.healthy = True

    async def generate(self, req: Any) -> Any:
        from app.generation.contract import GenerationResult, GenerationStatus, GenerationUsage

        self.calls.append(req)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return GenerationResult(
            status=GenerationStatus.succeeded,
            output=self.output,
            usage=GenerationUsage(
                model=self.usage_model,
                units=self.usage_units,
                unit_kind=self.unit_kind,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
            ),
            stop_reason="end",
            provider_ref=self.provider_ref,
        )

    async def poll(self, provider_ref: str) -> Any:
        raise NotImplementedError("FakeGenerationProvider is synchronous")

    async def healthcheck(self) -> Any:
        from app.generation.contract import ProviderHealth

        return ProviderHealth(healthy=self.healthy)


class FakeStoreKitVerifier:
    """Scriptable StoreKit verifier. COUNTS its calls — that counter is how the observability
    tests DERIVE ``money`` ("did a verifying step run before this branch?"), instead of reading
    the answer from a table in docs (R-OBS-6)."""

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None
        self.next_transaction: Any = None

    def script(
        self,
        *,
        transaction_id: str,
        product_id: str,
        expires_in_days: float | None = 30,
        revoked: bool = False,
        environment: str = "production",
    ) -> None:
        from app.subscription.storekit import VerifiedTransaction

        expires_at = (
            datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=expires_in_days)
            if expires_in_days is not None
            else None
        )
        self.error = None
        self.next_transaction = VerifiedTransaction(
            transaction_id=transaction_id,
            original_transaction_id=transaction_id,
            product_id=product_id,
            expires_at=expires_at,
            revoked=revoked,
            environment=environment,
        )

    def verify(self, signed_transaction: str) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.next_transaction is not None, "FakeStoreKitVerifier was not scripted"
        return self.next_transaction


@pytest.fixture
def fake_provider() -> FakeGenerationProvider:
    return FakeGenerationProvider()


@pytest.fixture
def fake_storekit() -> FakeStoreKitVerifier:
    return FakeStoreKitVerifier()


# --------------------------------------------------------------------------------------------
# Rate limiters: deterministic allow by default (Redis is absent; fail-open is not determinism)
# --------------------------------------------------------------------------------------------
_LIMIT_TARGETS: tuple[tuple[str, str], ...] = (
    ("app.api_gateway.rate_limit", "enforce_auth_limits"),
    ("app.api_gateway.rate_limit", "enforce_admin_limits"),
    ("app.api_gateway.rate_limit", "enforce_other_limits"),
    ("app.api_gateway.rate_limit", "enforce_generation_limits"),
    ("app.api_gateway.rate_limit", "enforce_cloudpayments_webhook_limits"),
    ("app.api_gateway.routers.auth", "enforce_auth_limits"),
    ("app.api_gateway.routers.admin", "enforce_admin_limits"),
    ("app.api_gateway.routers.billing", "enforce_other_limits"),
    ("app.api_gateway.routers.billing_webhooks", "enforce_other_limits"),
    ("app.api_gateway.routers.billing_webhooks", "enforce_cloudpayments_webhook_limits"),
    ("app.api_gateway.routers.generations", "enforce_other_limits"),
    ("app.api_gateway.routers.policy", "enforce_other_limits"),
    ("app.api_gateway.routers.wallet", "enforce_other_limits"),
    ("app.api_gateway.routers.profile", "enforce_other_limits"),
    ("app.domain.routers.generate", "enforce_generation_limits"),
)


def deny_limiter(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Force ONE limiter to refuse (429 tests). Patches every module that imported it by name."""
    import importlib

    async def _deny(**_kwargs: Any) -> bool:
        return False

    for module_name, attr in _LIMIT_TARGETS:
        if attr != name:
            continue
        monkeypatch.setattr(importlib.import_module(module_name), attr, _deny, raising=False)


@pytest.fixture
def allow_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    async def _allow(**_kwargs: Any) -> bool:
        return True

    for module_name, attr in _LIMIT_TARGETS:
        monkeypatch.setattr(importlib.import_module(module_name), attr, _allow, raising=False)


# --------------------------------------------------------------------------------------------
# The ASGI client
# --------------------------------------------------------------------------------------------
@pytest.fixture
async def client(
    sessionmaker_: async_sessionmaker[AsyncSession],
    fake_provider: FakeGenerationProvider,
    fake_storekit: FakeStoreKitVerifier,
    allow_limits: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """App wired to the container DB, with the two external boundaries faked."""
    from app import deps
    from app.domain.routers import generate as generate_router
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with sessionmaker_() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    # StoreKit: the singleton IS the boundary (deps calls get_storekit_verifier() per request).
    monkeypatch.setattr(storekit_mod, "_verifier_singleton", fake_storekit, raising=False)

    # Generation provider: patch where the name was imported (deps + the domain route).
    monkeypatch.setattr(deps, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(generate_router, "get_provider", lambda: fake_provider)

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.app = app  # type: ignore[attr-defined]  # tests reach for dependency_overrides
        yield ac


# --------------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------------
async def seed_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    trial_used: bool = True,
    subscription: str | None = None,
    expires_in_hours: float | None = 24,
    balance: int | None = None,
    device_id: str | None = None,
    plan: str = PRODUCT_SUB,
) -> uuid.UUID:
    """Insert a user (+ optional subscription / wallet / device). Commits.

    ``trial_used=True`` by DEFAULT: the free trial would otherwise silently pay for the first
    generation of every test and hide the debit under it.

    ``device_id`` is stored VERBATIM — the casing the caller passes is the casing in the DB. The
    resolve tests depend on that (a fixture that normalises the casing cannot catch the
    normalisation bug it exists for).
    """
    uid = user_id or uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, trial_used) VALUES (:id, :tu)"),
        {"id": str(uid), "tu": trial_used},
    )
    if subscription is not None:
        expires = None
        if expires_in_hours is not None:
            expires = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
                hours=expires_in_hours
            )
        await session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at) "
                "VALUES (:uid, CAST(:st AS subscription_status), :plan, :exp)"
            ),
            {"uid": str(uid), "st": subscription, "plan": plan, "exp": expires},
        )
    if balance is not None:
        await session.execute(
            text("INSERT INTO wallets (user_id, balance) VALUES (:uid, :bal)"),
            {"uid": str(uid), "bal": balance},
        )
    if device_id is not None:
        await session.execute(
            text("INSERT INTO auth_devices (user_id, device_id) VALUES (:uid, :did)"),
            {"uid": str(uid), "did": device_id},
        )
    await session.commit()
    return uid


async def balance_of(session: AsyncSession, user_id: uuid.UUID) -> int:
    value = await session.scalar(
        text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(user_id)}
    )
    return int(value or 0)


async def ledger_rows(session: AsyncSession, user_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "SELECT type, amount, reason, idempotency_key FROM ledger_transactions "
                "WHERE user_id = :uid ORDER BY created_at"
            ),
            {"uid": str(user_id)},
        )
    ).all()
    return [
        {"type": r[0], "amount": int(r[1]), "reason": r[2], "idempotency_key": r[3]} for r in rows
    ]


async def has_ledger_key(session: AsyncSession, user_id: uuid.UUID, key: str) -> bool:
    """``credited`` — READ FROM THE DATABASE, never from the branch that just ran (R-OBS-6)."""
    found = await session.scalar(
        text("SELECT 1 FROM ledger_transactions WHERE user_id = :uid AND idempotency_key = :key"),
        {"uid": str(user_id), "key": key},
    )
    return found is not None


def metric_value(name: str, **labels: str) -> float:
    """Current value of a Prometheus counter sample (0.0 when the series does not exist yet)."""
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(name, labels)
    return float(value) if value is not None else 0.0
