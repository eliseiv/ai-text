"""``CoreSettings`` — configuration of the CORE from env.

Rules:

1. **Not a single domain field lives here.** The source had one monolithic ``Settings`` with 102
   fields, ~45 of them domain-specific (``ANTHROPIC_*`` / ``ATTACHMENT_*`` / ``WORKSPACE_*`` …),
   which every new service inherited and could not remove without forking the core.
2. **The core never imports ``app.domain.*``** — not at module level and not inside a method. The
   source violated this (``config.py`` imported ``app.chat.presets``), which made the core
   unable to start without the domain. Enforced by ``test_core_does_not_import_domain.py``.
3. A domain subclasses this class (``DomainSettings(CoreSettings)``) and registers it via
   ``DomainRegistry.settings_cls``; ``get_settings()`` instantiates whatever it finds there.
4. ``extra="ignore"`` is REQUIRED for (3): without it ``CoreSettings`` would explode on any
   domain key present in ``.env``. Accepted cost: a typo in a core variable name is silently
   ignored — mitigated by fail-closed behaviour on the critical ones (no ``JWT_PRIVATE_KEY`` →
   ``503``; no ``ADMIN_API_SECRET`` → ``401``), so a typo surfaces immediately and loudly.

Config maps degrade gracefully (never a startup crash): malformed JSON → empty map → fail-closed
downstream (every purchase rejected). A mis-configuration must be loud in metrics/alerts, not
quietly compensated by a default amount (BR-8).
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.extensions.loader import load_registry
from app.products import Product, parse_products

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Default payment-freshness window (hours) for the CloudPayments reconciliation. A
# non-positive CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS falls back to this instead of disabling it.
_CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS = 72


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # required for DomainSettings inheritance
    )

    # --- Service / instance identity ---
    # The ONE place where the service names itself: OpenAPI title, label in logs/metrics.
    service_name: str = Field(default="service", alias="SERVICE_NAME")
    # Optional human-readable OpenAPI title; empty => service_name (see service_title_resolved()).
    service_title: str = Field(default="", alias="SERVICE_TITLE")
    service_version: str = Field(default="0.1.0", alias="SERVICE_VERSION")
    # Instance domain (Traefik Host + ACME); also read by the app to build absolute URLs.
    service_domain: str = Field(default="", alias="SERVICE_DOMAIN")
    environment: Literal["dev", "staging", "prod"] = Field(default="dev", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # /docs, /redoc, /openapi.json. Recommended false in prod.
    docs_enabled: bool = Field(default=True, alias="DOCS_ENABLED")

    # --- Storage ---
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/app",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    # Per-process pool. (DB_POOL_SIZE + DB_MAX_OVERFLOW) * workers * replicas MUST stay below
    # PostgreSQL max_connections.
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: float = Field(default=30.0, alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=1800, alias="DB_POOL_RECYCLE")

    # --- Auth / JWT (RS256 embedded issuer) ---
    jwt_issuer: str = Field(default="", alias="JWT_ISSUER")
    jwt_audience: str = Field(default="", alias="JWT_AUDIENCE")
    # SECRET. PEM string (\n-escaped) OR a file path (*_PATH wins). Absent => /v1/auth/* -> 503.
    jwt_private_key: str = Field(default="", alias="JWT_PRIVATE_KEY")
    jwt_private_key_path: str = Field(default="", alias="JWT_PRIVATE_KEY_PATH")
    jwt_public_key: str = Field(default="", alias="JWT_PUBLIC_KEY")
    jwt_public_key_path: str = Field(default="", alias="JWT_PUBLIC_KEY_PATH")
    jwt_kid: str = Field(default="", alias="JWT_KID")
    # Verify-only mode against an EXTERNAL issuer; unused by the embedded one.
    jwt_jwks_url: str = Field(default="", alias="JWT_JWKS_URL")
    jwks_cache_ttl_seconds: int = Field(default=300, alias="JWT_JWKS_CACHE_TTL")
    auth_access_ttl_seconds: int = Field(default=3600, alias="AUTH_ACCESS_TTL_SECONDS")
    auth_refresh_ttl_seconds: int = Field(default=2592000, alias="AUTH_REFRESH_TTL_SECONDS")
    auth_rate_limit_per_ip: int = Field(default=10, alias="AUTH_RATE_LIMIT_PER_IP")
    auth_jwks_enabled: bool = Field(default=True, alias="AUTH_JWKS_ENABLED")

    # --- Sign in with Apple ---
    apple_oidc_issuer: str = Field(default="https://appleid.apple.com", alias="APPLE_OIDC_ISSUER")
    apple_jwks_url: str = Field(
        default="https://appleid.apple.com/auth/keys", alias="APPLE_JWKS_URL"
    )
    # Expected `aud` = app bundle id. Empty => falls back to APPSTORE_BUNDLE_ID; both empty => 503.
    apple_audience: str = Field(default="", alias="APPLE_AUDIENCE")
    # HS256 test-mode for hermetic tests. Prod is fail-closed (HS256 outside test-mode => 401).
    apple_test_mode: bool = Field(default=False, alias="APPLE_TEST_MODE")
    apple_test_secret: str = Field(default="", alias="APPLE_TEST_SECRET")  # SECRET (test-only)

    # --- Apple StoreKit ---
    appstore_environment: str = Field(default="Production", alias="APPSTORE_ENVIRONMENT")
    appstore_bundle_id: str = Field(default="", alias="APPSTORE_BUNDLE_ID")
    # Apple root CA dir. Unset => the verifier refuses transactions (422, fail-closed).
    appstore_root_cert_dir: str = Field(default="", alias="APPSTORE_ROOT_CERT_DIR")
    storekit_test_mode: bool = Field(default=False, alias="STOREKIT_TEST_MODE")
    storekit_test_secret: str = Field(default="", alias="STOREKIT_TEST_SECRET")  # SECRET (test)

    # --- Billing — common ---
    # THE single product map: the only source of the credit amount (BR-8) + channel allowlist.
    products_raw: str = Field(default="{}", alias="PRODUCTS")
    # ONLY used as the default credit amount of the ADMIN subscription grant, where no
    # product exists by definition. NOT a fallback for payment channels — an unknown productId is
    # rejected with 0 credits.
    subscription_credits_per_period: int = Field(
        default=1000, alias="SUBSCRIPTION_CREDITS_PER_PERIOD"
    )
    trial_enabled: bool = Field(default=True, alias="TRIAL_ENABLED")

    # --- Billing — Adapty ---
    # SECRET. Bearer of POST /v1/billing/adapty/webhook (constant-time compare). Empty => 500.
    adapty_webhook_secret: str = Field(default="", alias="ADAPTY_WEBHOOK_SECRET")

    # --- Billing — CloudPayments / broadapps ---
    # Fixed aggregator host for OUR outgoing calls (anti-SSRF: never taken from a client body).
    cloudpayments_api_base: str = Field(default="", alias="CLOUDPAYMENTS_API_BASE")
    cloudpayments_app_id: str = Field(default="", alias="CLOUDPAYMENTS_APP_ID")
    # SECRET. Bearer of OUR outgoing calls (checkout + verify) and the channel activation gate:
    # empty => webhook 500, checkout 503.
    cloudpayments_api_token: str = Field(default="", alias="CLOUDPAYMENTS_API_TOKEN")
    # LEGACY, optional: no longer gates anything; only feeds the `matched` observability field.
    cloudpayments_webhook_token: str = Field(default="", alias="CLOUDPAYMENTS_WEBHOOK_TOKEN")
    cloudpayments_paid_statuses_raw: str = Field(
        default="succeeded", alias="CLOUDPAYMENTS_PAID_STATUSES"
    )
    # Aggregator statuses meaning "this payment was refunded" (CSV). The refund callback is only
    # a trigger, like the payment one: the refund itself is confirmed through verify.
    cloudpayments_refunded_statuses_raw: str = Field(
        default="refunded", alias="CLOUDPAYMENTS_REFUNDED_STATUSES"
    )
    cloudpayments_payment_freshness_hours: int = Field(
        default=_CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS,
        alias="CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS",
    )
    # Per-IP limit of the PUBLIC webhook (anti-amplification of the outgoing verify GET).
    cloudpayments_webhook_rate_limit_per_ip: int = Field(
        default=120, alias="CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP"
    )

    # --- Generation & pricing ---
    # `echo` = the built-in EchoProvider. A domain registers its own via
    # DomainRegistry.generation_provider and names it here.
    generation_provider: str = Field(default="echo", alias="GENERATION_PROVIDER")
    generation_timeout_seconds: float = Field(default=120.0, alias="GENERATION_TIMEOUT_SECONDS")
    # provider `output` lands in generations.meta ONLY below this size. The core stores no blobs.
    generation_meta_max_bytes: int = Field(default=8192, alias="GENERATION_META_MAX_BYTES")
    # Resource guard (step 1.5), NOT a money invariant. 0 disables it.
    generation_max_inflight_per_user: int = Field(
        default=3, alias="GENERATION_MAX_INFLIGHT_PER_USER"
    )
    pricing_mode: Literal["flat", "units", "tokens", "custom"] = Field(
        default="flat", alias="PRICING_MODE"
    )
    pricing_flat_credits: int = Field(default=1, alias="PRICING_FLAT_CREDITS")
    pricing_units_raw: str = Field(default="{}", alias="PRICING_UNITS")
    pricing_token_weights_raw: str = Field(default="{}", alias="PRICING_TOKEN_WEIGHTS")
    pricing_token_divisor: int = Field(default=1000, alias="PRICING_TOKEN_DIVISOR")
    # Anti-tamper ceiling on the price of ONE generation (BR-9): a provider returning an absurd
    # usage (units=10**9) can never drain the balance.
    pricing_max_credits_per_generation: int = Field(
        default=100, alias="PRICING_MAX_CREDITS_PER_GENERATION"
    )

    # --- Admin ---
    admin_api_secret: str = Field(default="", alias="ADMIN_API_SECRET")  # SECRET; empty => 401
    admin_api_secret_prev: str = Field(default="", alias="ADMIN_API_SECRET_PREV")  # rotation
    admin_rate_limit_per_min: int = Field(default=10, alias="ADMIN_RATE_LIMIT_PER_MIN")

    # --- Gateway / limits ---
    # General transport body limit. Per-path exceptions come from DomainRegistry.body_limit_rules;
    # the core knows no domain paths.
    size_limit_body: int = Field(default=512 * 1024, alias="SIZE_LIMIT_BODY")
    rate_limit_per_user: int = Field(default=60, alias="RATE_LIMIT_PER_USER")
    rate_limit_per_ip: int = Field(default=120, alias="RATE_LIMIT_PER_IP")
    # Alias (aligned with enforce_generation_limits and the
    # GENERATION_* family). Because of extra="ignore" a wrong alias here is SILENT: an
    # operator configuring the instance strictly by docs would get the value ignored and the limit
    # stuck at the default forever. The alias IS the contract.
    rate_limit_generation_per_user: int = Field(default=30, alias="RATE_LIMIT_GENERATION_PER_USER")
    # Sliding-window width shared by every limiter (seconds); the caps above are per window.
    rate_limit_window_seconds: int = Field(default=60, alias="RATE_LIMIT_WINDOW_SECONDS")
    # Empty => X-Forwarded-For is NEVER trusted (socket peer is used) — the safe default. In prod
    # this MUST contain the `web` docker-network subnet, otherwise per-IP rate limiting is broken
    # silently (every client looks like Traefik).
    trusted_proxy_ips: str = Field(default="", alias="TRUSTED_PROXY_IPS")
    trusted_proxy_hop_count: int = Field(default=1, alias="TRUSTED_PROXY_HOP_COUNT")

    # --- Observability ---
    metrics_scrape_token: str = Field(default="", alias="METRICS_SCRAPE_TOKEN")  # SECRET
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")

    # ------------------------------------------------------------------ derived / parsed values

    def service_title_resolved(self) -> str:
        """OpenAPI title: ``SERVICE_TITLE`` when set, otherwise ``SERVICE_NAME``.

        Keeps the api-gateway invariant "title = SERVICE_NAME" true by default while allowing a
        prettier human title without renaming the service in logs/metrics.
        """
        return self.service_title.strip() or self.service_name

    def products(self) -> dict[str, Product]:
        """The parsed ``PRODUCTS`` catalogue. Pure; cached via ``get_settings()``.

        Malformed JSON / non-object => ``{}`` => every purchase is rejected (fail-closed). See
        ``app.products.parse_products`` for the per-entry validation rules.
        """
        return parse_products(self.products_raw)

    def pricing_units(self) -> dict[str, float]:
        """``PRICING_UNITS``: ``{"<kind>": rate, "<kind>:<model>": rate}``. Degrades, never crashes.

        Only positive numeric rates survive (``bool`` excluded). A malformed document → ``{}`` →
        every rate falls back to 1. Degradation, not a crash: a mis-typed pricing map must not take
        auth, generation and ``/health`` down with it — the wrong price is visible in metrics.
        """
        import json

        try:
            parsed = json.loads(self.pricing_units_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        rates: dict[str, float] = {}
        for key, value in parsed.items():
            if not isinstance(key, str) or isinstance(value, bool):
                continue
            if isinstance(value, int | float) and value > 0:
                rates[key] = float(value)
        return rates

    def pricing_token_weights(self) -> dict[str, dict[str, float]]:
        """``PRICING_TOKEN_WEIGHTS``: ``{"<model>"|"default": {"input": w, "output": w, …}}``."""
        import json

        try:
            parsed = json.loads(self.pricing_token_weights_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        table: dict[str, dict[str, float]] = {}
        for model, weights in parsed.items():
            if not isinstance(model, str) or not isinstance(weights, dict):
                continue
            clean: dict[str, float] = {}
            for name, value in weights.items():
                if (
                    isinstance(name, str)
                    and not isinstance(value, bool)
                    and isinstance(value, int | float)
                    and value >= 0
                ):
                    clean[name] = float(value)
            if clean:
                table[model] = clean
        return table

    @field_validator("cloudpayments_payment_freshness_hours")
    @classmethod
    def _clamp_freshness_hours(cls, value: int) -> int:
        """A non-positive freshness window degrades to the default instead of disabling it."""
        return value if value > 0 else _CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS

    def cloudpayments_paid_statuses(self) -> frozenset[str]:
        """Aggregator statuses treated as "paid". CSV or JSON array, lower-cased.

        A malformed/empty value yields ``{"succeeded"}`` (the authoritative broadapps value) so
        the gate can never be accidentally emptied.
        """
        import json

        raw = (self.cloudpayments_paid_statuses_raw or "").strip()
        if not raw:
            return frozenset({"succeeded"})
        statuses: set[str] = set()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, str) and item.strip():
                        statuses.add(item.strip().lower())
        else:
            for part in raw.split(","):
                token = part.strip().lower()
                if token:
                    statuses.add(token)
        return frozenset(statuses) if statuses else frozenset({"succeeded"})

    def cloudpayments_refunded_statuses(self) -> frozenset[str]:
        """Aggregator statuses treated as "refunded". Empty/malformed → ``{"refunded"}``."""
        statuses = {
            part.strip().lower()
            for part in (self.cloudpayments_refunded_statuses_raw or "").split(",")
            if part.strip()
        }
        return frozenset(statuses) if statuses else frozenset({"refunded"})

    def cloudpayments_checkout_configured(self) -> bool:
        """True when the RU checkout is configured here: app id AND API token."""
        return bool(self.cloudpayments_app_id and self.cloudpayments_api_token)

    @staticmethod
    def _resolve_pem(path_value: str, string_value: str) -> str:
        """Resolve a PEM key: a file path wins over the ``\\n``-escaped string.

        A path is read from disk verbatim (prod: mounted secret, no escaping). Otherwise literal
        ``\\n`` sequences in the env string become real newlines, so a single-line ``.env`` value
        yields a valid multi-line PEM. Empty when neither is configured. Never logged (redaction
        covers ``*key*``).
        """
        if path_value:
            with open(path_value, encoding="utf-8") as handle:
                return handle.read()
        if string_value:
            return string_value.replace("\\n", "\n")
        return ""

    def resolve_private_key(self) -> str:
        """Private RS256 signing key PEM, or '' when the issuer is not configured (=> 503)."""
        return self._resolve_pem(self.jwt_private_key_path, self.jwt_private_key)

    def resolve_public_key(self) -> str:
        """Public RS256 verification key PEM (JwtVerifier + the JWKS endpoint)."""
        return self._resolve_pem(self.jwt_public_key_path, self.jwt_public_key)

    def apple_audience_resolved(self) -> str:
        """Effective Apple ``aud``: ``APPLE_AUDIENCE`` else ``APPSTORE_BUNDLE_ID``.

        Empty result => Apple sign-in is "not configured" => the router returns 503 (operational
        mis-configuration, not a client error).
        """
        explicit = self.apple_audience.strip()
        if explicit:
            return explicit
        return self.appstore_bundle_id.strip()

    def normalized_service_domain(self) -> str:
        """``SERVICE_DOMAIN`` as a bare ``host[:port]`` for building absolute URLs.

        Strips a leading http(s):// scheme (case-insensitive) and surrounding slashes, so the
        value is the same host however it was set. '' when unset => callers fall back to relative
        URLs (dev).
        """
        value = self.service_domain.strip()
        lowered = value.lower()
        if lowered.startswith("https://"):
            value = value[len("https://") :]
        elif lowered.startswith("http://"):
            value = value[len("http://") :]
        return value.strip("/")

    def trusted_proxy_networks(self) -> tuple[_IpNetwork, ...]:
        """Parse ``TRUSTED_PROXY_IPS`` (comma-separated IPs/CIDRs) into networks.

        Invalid entries are skipped. Empty/blank => empty tuple => X-Forwarded-For is never
        trusted (spoofable header).
        """
        networks: list[_IpNetwork] = []
        for raw in self.trusted_proxy_ips.split(","):
            entry = raw.strip()
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue
        return tuple(networks)


@lru_cache
def get_settings() -> CoreSettings:
    """Process-wide settings. Instantiates ``registry.settings_cls`` when a domain provides one.

    A domain subclasses ``CoreSettings``; the core keeps using it through the base type
    (Liskov). Cached — changing env at runtime is not picked up (12-factor); tests must call
    ``get_settings.cache_clear()``.
    """
    registry = load_registry()
    cls = registry.settings_cls or CoreSettings
    return cls()
