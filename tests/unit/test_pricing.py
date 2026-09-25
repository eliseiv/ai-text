"""Pricing (BR-9) — anti-tamper: the price never comes from the client or the output."""

from __future__ import annotations

import json

import pytest

from app.config import CoreSettings
from app.generation.contract import GenerationUsage
from app.generation.pricing import FlatPricing, TokensPricing, UnitsPricing, build_pricing


def settings(**overrides: object) -> CoreSettings:
    base: dict[str, object] = {
        "PRICING_MODE": "flat",
        "PRICING_FLAT_CREDITS": 1,
        "PRICING_UNITS": "{}",
        "PRICING_TOKEN_WEIGHTS": "{}",
        "PRICING_TOKEN_DIVISOR": 1000,
        "PRICING_MAX_CREDITS_PER_GENERATION": 100,
    }
    base.update(overrides)
    return CoreSettings(**base)  # type: ignore[arg-type]


def usage(**overrides: object) -> GenerationUsage:
    base: dict[str, object] = {"model": "m1", "units": 1, "unit_kind": "call"}
    base.update(overrides)
    return GenerationUsage(**base)  # type: ignore[arg-type]


# --- flat ------------------------------------------------------------------------------------
def test_flat_charges_the_configured_amount_regardless_of_usage() -> None:
    pricing = FlatPricing(settings(PRICING_FLAT_CREDITS=7))
    assert pricing.quote(kind="echo", model=None, params={}) == 7
    assert pricing.charge(kind="echo", model=None, usage=usage(units=999)) == 7


# --- units -----------------------------------------------------------------------------------
def test_units_multiplies_units_by_the_rate() -> None:
    pricing = UnitsPricing(settings(PRICING_UNITS=json.dumps({"image": 2})))
    assert pricing.charge(kind="image", model=None, usage=usage(units=3)) == 6


def test_units_rate_of_kind_model_overrides_the_rate_of_kind() -> None:
    pricing = UnitsPricing(settings(PRICING_UNITS=json.dumps({"image": 2, "image:xl": 5})))
    assert pricing.charge(kind="image", model="xl", usage=usage(units=2)) == 10
    assert pricing.charge(kind="image", model="sm", usage=usage(units=2)) == 4


def test_units_missing_rate_falls_back_to_one() -> None:
    pricing = UnitsPricing(settings(PRICING_UNITS="{}"))
    assert pricing.charge(kind="video", model=None, usage=usage(units=4)) == 4


def test_units_quote_ignores_client_params() -> None:
    # The pre-flight estimate must not be influenced by the client body.
    pricing = UnitsPricing(settings(PRICING_UNITS=json.dumps({"image": 2})))
    assert pricing.quote(kind="image", model=None, params={"units": 10_000}) == 2


def test_units_rounds_up() -> None:
    pricing = UnitsPricing(settings(PRICING_UNITS=json.dumps({"image": 0.4})))
    assert pricing.charge(kind="image", model=None, usage=usage(units=3)) == 2  # ceil(1.2)


# --- tokens ----------------------------------------------------------------------------------
def test_tokens_weighted_divided_and_rounded_up() -> None:
    pricing = TokensPricing(
        settings(
            PRICING_MODE="tokens",
            PRICING_TOKEN_WEIGHTS=json.dumps({"default": {"input": 1, "output": 3}}),
            PRICING_TOKEN_DIVISOR=1000,
        )
    )
    # (1000*1 + 500*3) / 1000 = 2.5 → 3
    price = pricing.charge(
        kind="text", model=None, usage=usage(input_tokens=1000, output_tokens=500)
    )
    assert price == 3


def test_tokens_model_specific_weights_win() -> None:
    pricing = TokensPricing(
        settings(
            PRICING_MODE="tokens",
            PRICING_TOKEN_WEIGHTS=json.dumps(
                {"default": {"input": 1, "output": 1}, "big": {"input": 10, "output": 10}}
            ),
            PRICING_TOKEN_DIVISOR=1000,
        )
    )
    assert pricing.charge(kind="text", model="big", usage=usage(input_tokens=1000)) == 10
    assert pricing.charge(kind="text", model=None, usage=usage(input_tokens=1000)) == 1


# --- the ceiling (anti-tamper) ---------------------------------------------------------------
@pytest.mark.parametrize(
    "pricing",
    [
        UnitsPricing(
            settings(PRICING_UNITS=json.dumps({"image": 1}), PRICING_MAX_CREDITS_PER_GENERATION=10)
        ),
        TokensPricing(
            settings(
                PRICING_TOKEN_WEIGHTS=json.dumps({"default": {"input": 1}}),
                PRICING_TOKEN_DIVISOR=1,
                PRICING_MAX_CREDITS_PER_GENERATION=10,
            )
        ),
        FlatPricing(settings(PRICING_FLAT_CREDITS=1000, PRICING_MAX_CREDITS_PER_GENERATION=10)),
    ],
)
def test_price_is_capped_in_every_mode(pricing: object) -> None:
    # A provider returning an absurd usage (units=10**9) must never drain the balance.
    charged = pricing.charge(  # type: ignore[attr-defined]
        kind="image", model=None, usage=usage(units=10**9, input_tokens=10**9)
    )
    assert charged == 10


def test_price_is_computed_from_usage_not_from_output() -> None:
    """Anti-tamper: a provider whose ``output`` contradicts its ``usage`` is priced by ``usage``.

    ``GenerationResult.output`` is not even an argument of ``charge()`` — the tamper vector does
    not exist by construction. This test pins that signature.
    """
    import inspect

    for cls in (FlatPricing, UnitsPricing, TokensPricing):
        params = set(inspect.signature(cls.charge).parameters)
        assert "output" not in params
        assert "credits" not in params
        assert "params" not in params  # the client body cannot reach the final price either


def test_quote_is_non_negative_and_used_for_the_policy_gate() -> None:
    for mode in ("flat", "units", "tokens"):
        pricing = build_pricing(settings(PRICING_MODE=mode))
        assert pricing.quote(kind="echo", model=None, params={}) >= 0


def test_build_pricing_selects_the_mode() -> None:
    assert isinstance(build_pricing(settings(PRICING_MODE="flat")), FlatPricing)
    assert isinstance(build_pricing(settings(PRICING_MODE="units")), UnitsPricing)
    assert isinstance(build_pricing(settings(PRICING_MODE="tokens")), TokensPricing)


def test_malformed_pricing_maps_degrade_instead_of_crashing() -> None:
    broken = settings(PRICING_UNITS="{not json", PRICING_TOKEN_WEIGHTS="[]")
    assert broken.pricing_units() == {}
    assert broken.pricing_token_weights() == {}
    # …and a broken map never yields a free or negative price.
    assert UnitsPricing(broken).charge(kind="image", model=None, usage=usage(units=2)) == 2


def test_negative_and_bool_rates_are_ignored() -> None:
    s = settings(PRICING_UNITS=json.dumps({"image": -5, "video": True, "audio": 2}))
    assert s.pricing_units() == {"audio": 2.0}
