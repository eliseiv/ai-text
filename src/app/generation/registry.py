"""Resolving the active provider and pricing policy.

The domain supplies both as DATA in ``DomainRegistry`` — the core never imports domain code.

**Fail-closed on an unknown provider.** ``GENERATION_PROVIDER`` naming a provider nobody registered
raises ``503``; the core NEVER falls back to some "default" upstream. A silent fallback would
generate with the wrong provider and bill the user for it.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.errors import ProviderNotConfiguredError
from app.extensions.loader import load_registry
from app.generation.contract import GenerationProvider, PricingPolicy
from app.generation.echo_provider import PROVIDER_NAME as ECHO_NAME
from app.generation.echo_provider import EchoProvider
from app.generation.pricing import build_pricing


@lru_cache
def get_provider() -> GenerationProvider:
    """The domain's provider, or the built-in ``EchoProvider`` when ``GENERATION_PROVIDER=echo``."""
    registry = load_registry()
    settings = get_settings()
    if registry.generation_provider is not None:
        provider = registry.generation_provider
        # The env value is the operator's statement of WHICH provider must run. If it disagrees
        # with what the domain registered, refusing is the only safe answer: generating with the
        # wrong upstream and charging for it is worse than a 503.
        if settings.generation_provider not in (provider.name, ECHO_NAME):
            raise ProviderNotConfiguredError(
                f"GENERATION_PROVIDER={settings.generation_provider!r} does not match the "
                f"registered provider {provider.name!r}"
            )
        return provider
    if settings.generation_provider == ECHO_NAME:
        return EchoProvider()
    raise ProviderNotConfiguredError(
        f"no provider registered for GENERATION_PROVIDER={settings.generation_provider!r}"
    )


@lru_cache
def get_pricing() -> PricingPolicy:
    """The domain's pricing policy, else the built-in ``flat`` / ``units`` / ``tokens``.

    ``PRICING_MODE=custom`` without a registered policy is a mis-configuration, not a reason to
    quietly price everything at the flat rate.
    """
    registry = load_registry()
    settings = get_settings()
    if registry.pricing_policy is not None:
        return registry.pricing_policy
    if settings.pricing_mode == "custom":
        raise ProviderNotConfiguredError(
            "PRICING_MODE=custom requires DomainRegistry.pricing_policy"
        )
    return build_pricing(settings)
