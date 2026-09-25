"""``EchoProvider`` — the reference ``GenerationProvider``, zero external dependencies.

Three jobs:

1. ``POST /v1/generate`` WORKS the moment ``docker compose up`` finishes — the template is a running
   service, not a scaffold;
2. it is the worked example a domain copies (a provider is ~20 lines; everything else — policy,
   idempotency, pricing, debit, accounting, metrics, audit — the core already does);
3. it is the fixture of the reusable contract-suite.

Note what it does NOT do, because those are the two rules that keep billing correct: it never
touches the DB or the wallet, and it never names its own price (``GenerationResult`` has no
``credits`` field at all).
"""

from __future__ import annotations

from app.generation.contract import (
    GenerationRequest,
    GenerationResult,
    GenerationStatus,
    GenerationUsage,
    ProviderHealth,
)

PROVIDER_NAME = "echo"


class EchoProvider:
    """Returns the request ``params`` as the ``output``. One unit of work per call."""

    name = PROVIDER_NAME
    kind = "echo"

    async def generate(self, req: GenerationRequest) -> GenerationResult:
        return GenerationResult(
            status=GenerationStatus.succeeded,
            output={"echo": req.params},
            # units >= 1 is a CONTRACT requirement: a usage of all zeros prices to 0, i.e. free
            # generations. The contract-suite rejects a provider that reports nothing.
            usage=GenerationUsage(model=req.model or PROVIDER_NAME, units=1, unit_kind="call"),
            stop_reason="end",
        )

    async def poll(self, provider_ref: str) -> GenerationResult:
        # Sync-only provider. The async path exists in the schema and the API from day one, but a
        # provider that does not implement it must say so loudly rather than fake a result.
        raise NotImplementedError("EchoProvider is synchronous")

    async def healthcheck(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="echo provider has no upstream")
