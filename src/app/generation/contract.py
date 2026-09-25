"""The generation plug-in contract.

**Types only — no runtime service.** This module exists already at Ф2 because
``DomainRegistry.generation_provider`` / ``.pricing_policy`` must be typed; the runtime
(registry of providers, pricing implementations, repository, service, ``EchoProvider``) lands in
Ф6 alongside it.

Two hard rules that the whole billing correctness rests on:

* **``ProviderError`` is the ONLY way to report an upstream failure.** A provider that returns
  ``GenerationResult(status=failed)`` instead of raising — or raises a bare ``TimeoutError`` —
  breaks billing: the core cannot tell "it failed" from "it successfully generated nothing", and
  would charge for a failure (BR-7). Checked by the contract test-suite.
* **A provider never names its own price.** It reports ``usage``; the price comes from the
  server-side ``PRICING_*`` maps through ``PricingPolicy`` (BR-9 anti-tamper).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


class GenerationStatus(str, Enum):
    """Lifecycle of a generation (mirrors the ``generation_status`` enum in the DB).

    ``pending`` exists from day one (with ``provider_ref`` + ``poll()``) so the async path can be
    added later without a migration or an API change.
    """

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


@dataclass(frozen=True)
class GenerationUsage:
    """What the provider actually consumed/produced.

    Tokens — for LLM-like domains. ``units`` — for everything else (images, seconds, pages).
    The domain MUST fill this: an all-zero usage prices to 0 (free generations). The contract
    suite requires ``units >= 1`` OR non-zero tokens.
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    units: int = 1
    unit_kind: str = "call"  # "image" | "second" | "page" | "call" | ...


@dataclass(frozen=True)
class GenerationRequest:
    """What the core hands to the provider. ``params`` is opaque to the core."""

    user_id: UUID
    kind: str
    idempotency_key: str
    params: dict[str, Any]
    model: str | None
    request_id: str
    deadline_s: float


@dataclass(frozen=True)
class GenerationResult:
    """What the provider returns on success.

    ``output`` is a SMALL json document. Blobs (images/audio/video) go to the domain's own
    storage; only a link comes back here — the core never stores blobs.
    """

    status: GenerationStatus
    output: dict[str, Any]
    usage: GenerationUsage
    stop_reason: str | None = None
    provider_ref: str | None = None  # upstream job id (entry point of a future async callback)
    raw_meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderHealth:
    """Result of ``GenerationProvider.healthcheck()``."""

    healthy: bool
    detail: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """THE single channel for reporting an upstream failure.

    Raising it means: the provider did not deliver ⇒ **0 credits** (BR-7, enforced by
    ``ck_generations_charge_only_succeeded`` at DB level as well).
    """

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        status_code: int | None = None,
        provider_error_type: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.provider_error_type = provider_error_type
        self.retryable = retryable
        super().__init__(message or code)


@runtime_checkable
class GenerationProvider(Protocol):
    """The plug-in a domain implements. The core calls nothing else on the upstream."""

    name: str
    kind: str

    async def generate(self, req: GenerationRequest) -> GenerationResult:
        """Run the generation. Failure => raise ``ProviderError`` (never return ``failed``)."""
        ...

    async def poll(self, provider_ref: str) -> GenerationResult:
        """Async path only; a sync-only provider raises ``NotImplementedError``."""
        ...

    async def healthcheck(self) -> ProviderHealth:
        """Liveness of the upstream (diagnostics; not on the request hot path)."""
        ...


class PricingPolicy(Protocol):
    """How much a generation costs. NEVER from the client body, NEVER from provider output."""

    def quote(self, *, kind: str, model: str | None, params: dict[str, Any]) -> int:
        """Estimate BEFORE the call — for the policy pre-flight balance check."""
        ...

    def charge(self, *, kind: str, model: str | None, usage: GenerationUsage) -> int:
        """Final price AFTER the call, from the provider's actual ``usage``, capped by
        ``PRICING_MAX_CREDITS_PER_GENERATION`` (BR-9)."""
        ...
