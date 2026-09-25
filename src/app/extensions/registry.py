"""``DomainRegistry`` — the one mechanism by which a domain plugs into the core.

THE central abstraction of the template. Every place where the source hardcoded knowledge of its
domain (the static list of 18 routers in ``main.py``, ``title="claude-ios-backend"``, the
``/v1/chat/run`` / ``/v1/workspaces/*/files`` paths inside ``SizeLimitMiddleware``, the
``/v1/preview/`` exemption in ``SecurityHeadersMiddleware``, the BYOK branches in the policy
engine, the LLM-client factory, the 45 domain fields in ``Settings``, the truncate table list in
``conftest.py``, the domain branches in ``lifespan``) is a place a new service would have had to
EDIT — i.e. fork the core. Here they all read this frozen dataclass instead.

Hard invariant: **the core never imports ``app.domain.*``.** The dependency is strictly one-way
(``app.domain -> app``, never back), so the domain reaches the core only as DATA passed into this
registry. ``test_core_does_not_import_domain.py`` (AST scan) enforces it.

All fields are optional: the empty template is fully functional with ``DomainRegistry()``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # typing-only imports — no runtime dependency, no import cycle
    from fastapi import APIRouter

    from app.config import CoreSettings
    from app.generation.contract import GenerationProvider, PricingPolicy
    from app.policy.engine import Decision, PolicyState

# Startup/shutdown hook: an awaitable callable taking nothing.
Hook = Callable[[], Awaitable[None]]

_DEFAULT_BODY_LIMIT_METHODS: tuple[str, ...] = ("POST", "PUT", "PATCH")


@dataclass(frozen=True)
class BodyLimitRule:
    """A per-path transport body limit.

    ``match`` is either an exact path (``/v1/image/upload``) or a prefix+suffix pattern with a
    single ``*`` standing for one or more path characters (``/v1/workspaces/*/files``). No regex:
    this runs on EVERY request, and a regex here buys nothing but catastrophic-backtracking risk.

    Precision matters: ``/v1/workspaces/*/files`` must match the UPLOAD but not
    ``/v1/workspaces/{id}`` (CRUD) nor ``/v1/workspaces/{id}/files/{file_id}`` (delete) —
    otherwise the raised limit leaks onto routes that do not need it, widening the memory-DoS
    surface. ``methods`` is the second line of defence (a GET carries no body).

    Consistency invariant: ``limit >= application_max_bytes * 4/3 + 256 KB`` (base64 inflates by
    4/3). Violating it reproduces the source's real bug — an advertised 8 MB upload limit that
    the gateway cut at 512 KB *before* the application validator ever saw the request.
    """

    match: str
    limit: int  # bytes
    methods: tuple[str, ...] = _DEFAULT_BODY_LIMIT_METHODS

    def matches(self, path: str, method: str) -> bool:
        if self.methods and method.upper() not in self.methods:
            return False
        if "*" not in self.match:
            return path == self.match
        prefix, suffix = self.match.split("*", 1)
        if not path.startswith(prefix) or not path.endswith(suffix):
            return False
        # The `*` must stand for at least one character (no empty id segment).
        return len(path) > len(prefix) + len(suffix)


class PolicyGate(Protocol):
    """A domain check executed AFTER the core ``evaluate()``.

    Returns ``None`` ("nothing to say") or a ``Decision``. A gate can only TIGHTEN the core
    verdict: an ``allowed`` decision from a gate NEVER overrides a core block (``apply_gates()``
    enforces this). Otherwise a domain could bypass billing — "my gate allows generation without
    a subscription".
    """

    def check(self, state: PolicyState, ctx: Mapping[str, Any]) -> Decision | None: ...


@dataclass(frozen=True)
class DomainRegistry:
    """Everything a domain contributes to the core, as data. All fields optional."""

    # --- HTTP surface ---
    routers: tuple[APIRouter, ...] = ()
    openapi_tags: tuple[dict[str, str], ...] = ()
    api_description: str | None = None

    # --- Middleware rules (declarative, instead of hardcoded paths in the core) ---
    body_limit_rules: tuple[BodyLimitRule, ...] = ()
    security_headers_exempt_prefixes: tuple[str, ...] = ()

    # --- Business logic ---
    policy_gates: tuple[PolicyGate, ...] = ()
    generation_provider: GenerationProvider | None = None
    pricing_policy: PricingPolicy | None = None

    # --- Configuration: the domain subclasses CoreSettings ---
    settings_cls: type[CoreSettings] | None = None

    # --- Observability: extra metric module(s) the domain wants imported/registered ---
    metrics_module: str | None = None

    # --- Tests: domain tables to truncate between cases (else they leak across tests) ---
    truncate_tables: tuple[str, ...] = ()

    # --- Lifecycle ---
    on_startup: tuple[Hook, ...] = field(default=())
    on_shutdown: tuple[Hook, ...] = field(default=())


# The registry an empty template (or a broken domain import) runs on. Fully functional.
EMPTY_REGISTRY = DomainRegistry()
