"""Adapty payload parsing — the REAL format, not the documented one.

All three assumptions of the original ADR were wrong on prod data, and each cost an incident:
the event id lives in ``profile_event_id``, the event names are undocumented, and one purchase
emits SEVERAL events sharing ONE ``transaction_id``.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.billing_adapty import parser
from app.billing_adapty.parser import ParsedEvent


def _event(**overrides: Any) -> ParsedEvent:
    base: dict[str, Any] = {
        "event_id": "e1",
        "event_type": "subscription_started",
        "customer_user_id": "device-1",
        "vendor_product_id": "sub.monthly",
        "expires_at": None,
        "transaction_id": "t1",
        "original_transaction_id": "o1",
        "is_active": None,
        "access_level_id": None,
        "will_renew": None,
    }
    base.update(overrides)
    return ParsedEvent(**base)


# --- ids ---------------------------------------------------------------------------------------
def test_event_id_prefers_profile_event_id() -> None:
    # THE bug: reading `event_id` first made the service ignore EVERY real Adapty event.
    body = {"profile_event_id": "pe-1", "event_id": "legacy"}
    assert parser.parse_event_id(body) == "pe-1"


def test_event_id_falls_back_and_accepts_integers() -> None:
    assert parser.parse_event_id({"event_id": 12345}) == "12345"
    assert parser.parse_event_id({"id": "x"}) == "x"
    assert parser.parse_event_id({}) is None


def test_event_id_ignores_booleans() -> None:
    assert parser.parse_event_id({"profile_event_id": True}) is None


def test_transaction_id_is_the_period_id_not_the_original() -> None:
    body = {"event_properties": {"transaction_id": "t2", "original_transaction_id": "o1"}}
    assert parser.parse_transaction_id(body) == "t2"
    assert parser.parse_original_transaction_id(body) == "o1"


def test_customer_user_id_is_kept_as_a_string() -> None:
    # It is usually a deviceId — parsing it as a UUID would discard resolvable identifiers.
    assert parser.parse_customer_user_id({"customer_user_id": "ABC-device"}) == "ABC-device"
    assert parser.parse_customer_user_id({"profile": {"customer_user_id": "d2"}}) == "d2"
    assert parser.parse_customer_user_id({}) is None


def test_vendor_product_id_is_read_from_event_properties_first() -> None:
    body = {"event_properties": {"vendor_product_id": "p1"}, "product_id": "p2"}
    assert parser.parse_vendor_product_id(body) == "p1"


# --- expires_at --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2030-01-01T00:00:00Z", datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)),
        ("2030-01-01T00:00:00+00:00", datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)),
        ("2030-01-01T00:00:00", datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)),
    ],
)
def test_expires_at_parses_iso_and_is_always_aware(raw: str, expected: datetime.datetime) -> None:
    assert parser.parse_expires_at({"event_properties": {"expires_at": raw}}) == expected


def test_unparseable_expires_at_is_none_and_never_raises() -> None:
    assert parser.parse_expires_at({"event_properties": {"expires_at": "yesterday"}}) is None
    assert parser.parse_expires_at({}) is None


def test_flags_require_real_json_booleans() -> None:
    assert parser.parse_is_active({"event_properties": {"is_active": True}}) is True
    assert parser.parse_is_active({"event_properties": {"is_active": "true"}}) is None
    assert parser.parse_will_renew({"will_renew": False}) is False


# --- classification: THREE classes, not two ----------------------------------------------------
@pytest.mark.parametrize("event_type", sorted(parser.GRANTING_EVENTS))
def test_granting_events(event_type: str) -> None:
    assert parser.classify_event(_event(event_type=event_type)) == parser.SEM_GRANTING


@pytest.mark.parametrize("event_type", sorted(parser.EXPIRING_EVENTS))
def test_expiring_events(event_type: str) -> None:
    assert parser.classify_event(_event(event_type=event_type)) == parser.SEM_EXPIRING


@pytest.mark.parametrize("event_type", sorted(parser.NOOP_EVENTS))
def test_renewal_cancelled_is_a_noop_and_never_revokes_access(event_type: str) -> None:
    """Turning auto-renew off is NOT a refund: the user keeps the period he paid for.

    Classifying it as `expiring` would take away access the customer already bought — the
    difference between a happy customer and a robbed one.
    """
    assert parser.classify_event(_event(event_type=event_type)) == parser.SEM_NOOP


def test_access_level_updated_is_conditional() -> None:
    premium_active = _event(
        event_type="access_level_updated", is_active=True, access_level_id="premium"
    )
    inactive = _event(event_type="access_level_updated", is_active=False)
    ambiguous = _event(event_type="access_level_updated", is_active=None, access_level_id=None)

    assert parser.classify_event(premium_active) == parser.SEM_GRANTING
    assert parser.classify_event(inactive) == parser.SEM_EXPIRING
    # Ambiguous signal → NOOP: never revoke on a maybe (the user may well have paid).
    assert parser.classify_event(ambiguous) == parser.SEM_NOOP


def test_unknown_event_type_is_a_noop_not_an_expiry() -> None:
    assert parser.classify_event(_event(event_type="some_new_platform_event")) == parser.SEM_NOOP


def test_event_type_is_lowercased() -> None:
    assert parser.parse_event_type({"event_type": "Subscription_Started"}) == "subscription_started"
    assert parser.parse_event_type({}) == ""
