"""Pricing — how much a generation costs.

**Anti-tamper (BR-9), the whole point of this module.** The price NEVER comes from:

* the client body (``{"credits": 0}`` is an obvious hole);
* the provider's ``output`` (a provider is domain code talking to an external API — letting it
  price itself means letting a compromised upstream zero out the billing).

Only: server-side ``PRICING_*`` maps × the ``usage`` the provider reported, and then the CEILING
``PRICING_MAX_CREDITS_PER_GENERATION`` — applied in EVERY mode, because a buggy provider returning
``units=10**9`` would otherwise drain the whole balance in one call.

``quote()`` before the call, ``charge()`` after it. Without ``quote()`` a user with 1 credit would
start a 3-credit generation: the provider would run (real money spent upstream), and the debit
would then find nothing — forcing either a negative balance (forbidden by CHECK) or a free result.
"""

from __future__ import annotations

import math
from typing import Any

from app.config import CoreSettings
from app.generation.contract import GenerationUsage

_DEFAULT_WEIGHTS_KEY = "default"


def _cap(credits: int, settings: CoreSettings) -> int:
    """The ceiling applies in EVERY mode — it is the anti-tamper guard, not a mode feature."""
    return max(0, min(credits, settings.pricing_max_credits_per_generation))


class FlatPricing:
    """``flat`` (default): one generation = ``PRICING_FLAT_CREDITS``.

    Reproduces the source's rule "1 credit = 1 message" literally, so a simple domain pays no
    complexity for a feature it does not need.
    """

    def __init__(self, settings: CoreSettings) -> None:
        self._settings = settings

    def quote(self, *, kind: str, model: str | None, params: dict[str, Any]) -> int:
        return _cap(self._settings.pricing_flat_credits, self._settings)

    def charge(self, *, kind: str, model: str | None, usage: GenerationUsage) -> int:
        return _cap(self._settings.pricing_flat_credits, self._settings)


class UnitsPricing:
    """``units``: images, seconds of video, pages. ``ceil(units × rate)``.

    ``rate`` lookup order: ``PRICING_UNITS["{kind}:{model}"]`` → ``PRICING_UNITS[kind]`` → ``1``.
    The model-specific key wins, so an expensive model can cost more without touching code.
    """

    def __init__(self, settings: CoreSettings) -> None:
        self._settings = settings

    def _rate(self, kind: str, model: str | None) -> float:
        rates = self._settings.pricing_units()
        if model and f"{kind}:{model}" in rates:
            return rates[f"{kind}:{model}"]
        return rates.get(kind, 1.0)

    def quote(self, *, kind: str, model: str | None, params: dict[str, Any]) -> int:
        # Before the call the real unit count is unknown → assume one unit. Deliberately NOT read
        # from `params`: the client must not be able to influence the pre-flight estimate.
        return _cap(math.ceil(self._rate(kind, model)), self._settings)

    def charge(self, *, kind: str, model: str | None, usage: GenerationUsage) -> int:
        return _cap(math.ceil(max(usage.units, 0) * self._rate(kind, model)), self._settings)


class TokensPricing:
    """``tokens``: LLM-like domains. ``ceil(weighted_tokens / PRICING_TOKEN_DIVISOR)``.

    ``quote()`` is deliberately conservative (the real token count only exists after the call).
    A too-low quote is survivable: the generation ends ``succeeded`` with
    ``billing_kind='unbilled'`` — the service works for free, the user never goes negative — and
    THAT path is now alerted (``impact='revenue_loss'``).
    """

    def __init__(self, settings: CoreSettings) -> None:
        self._settings = settings

    def _weights(self, model: str | None) -> dict[str, float]:
        table = self._settings.pricing_token_weights()
        if model and model in table:
            return table[model]
        return table.get(_DEFAULT_WEIGHTS_KEY, {"input": 1.0, "output": 1.0})

    def quote(self, *, kind: str, model: str | None, params: dict[str, Any]) -> int:
        return _cap(self._settings.pricing_flat_credits, self._settings)

    def charge(self, *, kind: str, model: str | None, usage: GenerationUsage) -> int:
        w = self._weights(model or usage.model)
        weighted = (
            usage.input_tokens * w.get("input", 1.0)
            + usage.output_tokens * w.get("output", 1.0)
            + usage.cache_read_tokens * w.get("cache_read", 0.0)
            + usage.cache_write_tokens * w.get("cache_write", 0.0)
        )
        divisor = max(self._settings.pricing_token_divisor, 1)
        return _cap(math.ceil(weighted / divisor), self._settings)


def build_pricing(settings: CoreSettings) -> FlatPricing | UnitsPricing | TokensPricing:
    """Build the built-in policy for ``PRICING_MODE``. ``custom`` is resolved by the registry."""
    if settings.pricing_mode == "units":
        return UnitsPricing(settings)
    if settings.pricing_mode == "tokens":
        return TokensPricing(settings)
    return FlatPricing(settings)
