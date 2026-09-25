"""PRODUCTS — the single catalogue (BR-8). Fail-closed, degrade-never-crash."""

from __future__ import annotations

import json
import logging

from app.products import Product, credits_for, parse_products


def test_valid_entry_is_parsed() -> None:
    products = parse_products(
        json.dumps({"p1": {"kind": "tokens", "credits": 100, "channels": ["adapty"]}})
    )
    assert products["p1"] == Product(
        product_id="p1",
        kind="tokens",
        credits=100,
        channels=frozenset({"adapty"}),
        title="p1",  # title defaults to the product id
    )


def test_malformed_json_yields_an_empty_catalogue_with_a_warning(
    caplog: object,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.products"):  # type: ignore[attr-defined]
        assert parse_products("{not json") == {}
    assert any("PRODUCTS" in r.message for r in caplog.records)  # type: ignore[attr-defined]


def test_non_object_json_yields_an_empty_catalogue() -> None:
    assert parse_products("[1, 2, 3]") == {}
    assert parse_products('"a string"') == {}


def test_invalid_entries_are_dropped_and_the_rest_keeps_working() -> None:
    raw = json.dumps(
        {
            "good": {"kind": "tokens", "credits": 10, "channels": ["adapty"]},
            "bad_kind": {"kind": "coffee", "credits": 10, "channels": ["adapty"]},
            "bad_credits_negative": {"kind": "tokens", "credits": -1, "channels": ["adapty"]},
            "bad_credits_zero": {"kind": "tokens", "credits": 0, "channels": ["adapty"]},
            "bad_credits_bool": {"kind": "tokens", "credits": True, "channels": ["adapty"]},
            "bad_channel": {"kind": "tokens", "credits": 10, "channels": ["paypal"]},
            "no_channels": {"kind": "tokens", "credits": 10, "channels": []},
            "not_an_object": "nope",
        }
    )
    products = parse_products(raw)
    assert set(products) == {"good"}


def test_true_never_becomes_one_credit() -> None:
    # bool is a subclass of int in Python — a silent `True → 1 credit` would be a real surprise.
    products = parse_products(
        json.dumps({"p": {"kind": "tokens", "credits": True, "channels": ["adapty"]}})
    )
    assert products == {}


def test_duplicate_valid_channel_does_not_drop_the_product() -> None:
    products = parse_products(
        json.dumps({"p": {"kind": "tokens", "credits": 5, "channels": ["adapty", "adapty"]}})
    )
    assert products["p"].channels == frozenset({"adapty"})


def test_credits_for_is_fail_closed_on_unknown_product_and_foreign_channel(
    monkeypatch: object,
) -> None:
    import app.products as products_mod

    catalogue = parse_products(
        json.dumps(
            {"apple.only": {"kind": "tokens", "credits": 50, "channels": ["apple_storekit"]}}
        )
    )
    monkeypatch.setattr(products_mod, "get_products", lambda: catalogue)  # type: ignore[attr-defined]

    assert credits_for("apple.only", "apple_storekit") == 50
    # A CloudPayments callback naming an Apple productId gets NOTHING (the source's hole).
    assert credits_for("apple.only", "cloudpayments") is None
    assert credits_for("unknown", "apple_storekit") is None
    assert credits_for(None, "apple_storekit") is None
