"""The reusable ``GenerationProvider`` contract suite.

A domain plugs its provider in with one line and gets a guarantee that it embeds correctly into
billing::

    from tests.contract.provider_suite import provider_contract_suite
    from app.domain.provider import FluxProvider

    test_flux = provider_contract_suite(FluxProvider(), kind="image")

The suite checks the properties billing correctness rests on — above all: an upstream failure is
reported ONLY by raising ``ProviderError``. A provider that returns ``status=failed`` (or ``None``)
makes the core unable to tell "it failed" from "it successfully generated nothing", and the core
would charge for a failure.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from app.generation.contract import (
    GenerationRequest,
    GenerationResult,
    GenerationStatus,
    ProviderError,
    ProviderHealth,
)


def make_request(kind: str, **overrides: Any) -> GenerationRequest:
    payload: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "kind": kind,
        "idempotency_key": "contract-suite-key",
        "params": {"prompt": "hello"},
        "model": None,
        "request_id": "req-1",
        "deadline_s": 5.0,
    }
    payload.update(overrides)
    return GenerationRequest(**payload)


def provider_contract_suite(
    provider: Any,
    *,
    kind: str,
    fail_upstream: Callable[[], None] | None = None,
) -> type:
    """Build a test class asserting ``provider`` satisfies the contract.

    ``fail_upstream`` (optional): a callable that puts the provider into "the upstream is down"
    state. When omitted, the failure-path checks are skipped for that provider — but the ones that
    can be checked without an upstream still run.
    """

    class ProviderContract:
        async def test_generate_succeeds_with_usable_usage(self) -> None:
            result = await provider.generate(make_request(kind))
            assert isinstance(result, GenerationResult)
            assert result.status is GenerationStatus.succeeded
            # (1) + (2): a usage that prices to nothing means free generations forever.
            assert result.usage.model, "usage.model must be non-empty (PricingPolicy keys on it)"
            has_units = result.usage.units >= 1
            has_tokens = (result.usage.input_tokens + result.usage.output_tokens) > 0
            assert has_units or has_tokens, "usage must carry units >= 1 or non-zero tokens"

        async def test_output_is_json_serializable(self) -> None:
            result = await provider.generate(make_request(kind))
            json.dumps(result.output)  # (5) — the core stores this in JSONB

        async def test_provider_ref_is_none_or_stable_string(self) -> None:
            result = await provider.generate(make_request(kind))
            assert result.provider_ref is None or isinstance(result.provider_ref, str)  # (6)

        async def test_request_is_frozen_and_not_mutated(self) -> None:
            req = make_request(kind)
            await provider.generate(req)
            # (9) — the request is a frozen dataclass; a provider cannot rewrite what it was asked.
            assert dataclasses.is_dataclass(req)
            with pytest.raises(dataclasses.FrozenInstanceError):
                req.kind = "tampered"  # type: ignore[misc]

        async def test_poll_is_implemented_or_raises_not_implemented(self) -> None:
            # (7) — a sync-only provider must SAY so, never return silent garbage.
            try:
                result = await provider.poll("ref-1")
            except NotImplementedError:
                return
            assert isinstance(result, GenerationResult)

        async def test_healthcheck_never_raises(self) -> None:
            health = await provider.healthcheck()  # (8)
            assert isinstance(health, ProviderHealth)

        async def test_no_credits_field_on_the_result(self) -> None:
            # (10) — a provider never names its own price (BR-9 anti-tamper).
            fields = {f.name for f in dataclasses.fields(GenerationResult)}
            assert "credits" not in fields and "price" not in fields

        async def test_upstream_failure_raises_provider_error(self) -> None:
            if fail_upstream is None:
                pytest.skip("provider does not expose an upstream-failure seam")
            fail_upstream()
            with pytest.raises(ProviderError) as exc_info:  # (3)
                await provider.generate(make_request(kind))
            assert isinstance(exc_info.value.retryable, bool)  # (4)

    ProviderContract.__name__ = f"TestProviderContract_{getattr(provider, 'name', 'provider')}"
    return ProviderContract
