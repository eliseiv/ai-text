"""Defensive parsing of the REAL Adapty payload.

The parser looks "dirty" on purpose. The base ADR was written from Adapty's documentation, and all
three of its assumptions turned out to be wrong on real prod data:

| assumed                     | reality                                | the bug it caused         |
|-----------------------------|----------------------------------------|---------------------------|
| event id in ``event_id``    | ``profile_event_id``                   | EVERY event ignored       |
| documented event names      | ``trial_started`` / ``access_level_*`` | mapping knew none of them |
| grant keyed by the event id | one purchase = SEVERAL events, one txn | double / triple crediting |

So: several possible locations per field, ids accepted as ``int`` OR ``str``, both
``event_properties.*`` and flat fallbacks, and an unknown value is a WARNING — never a crash. We do
not control this format and the platform changes it without telling us.

**The classification has THREE classes, not two.** Reading ``*_cancelled`` as "revoke access" would
take away a period the user has PAID FOR — turning off auto-renew is not a refund. ``NOOP`` is that
distinction, and it is the difference between a happy customer and a robbed one.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

GRANTING_EVENTS = frozenset({"trial_started", "subscription_started", "subscription_renewed"})
EXPIRING_EVENTS = frozenset({"subscription_expired", "subscription_cancelled"})
NOOP_EVENTS = frozenset({"subscription_renewal_cancelled", "trial_renewal_cancelled"})
CONDITIONAL_EVENTS = frozenset({"access_level_updated"})
# A consumable (token pack) bought through the Adapty SDK.
TOKEN_EVENTS = frozenset({"non_subscription_purchase"})
# Money returned to the user by the store: take back what the purchase granted.
REFUND_EVENTS = frozenset({"subscription_refunded", "non_subscription_purchase_refunded"})
KNOWN_EVENTS = (
    GRANTING_EVENTS
    | EXPIRING_EVENTS
    | NOOP_EVENTS
    | CONDITIONAL_EVENTS
    | TOKEN_EVENTS
    | REFUND_EVENTS
)

SEM_GRANTING = "granting"
SEM_EXPIRING = "expiring"
SEM_NOOP = "noop"
SEM_TOKENS = "tokens"
SEM_REFUND = "refund"

ACCESS_LEVEL_PREMIUM = "premium"


@dataclass(frozen=True)
class ParsedEvent:
    """One defensively parsed Adapty event.

    ``event_id`` (= ``profile_event_id``) is the DELIVERY key; ``transaction_id`` is the GRANT key.
    They are different on purpose — see ``billing/payments.py``.
    """

    event_id: str
    event_type: str
    customer_user_id: str  # usually a deviceId, NOT our userId — resolved via auth_devices
    vendor_product_id: str | None
    expires_at: datetime.datetime | None
    transaction_id: str | None
    original_transaction_id: str | None
    is_active: bool | None
    access_level_id: str | None
    will_renew: bool | None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_str(*candidates: Any) -> str | None:
    """First non-empty string, or a non-bool int coerced to str.

    Adapty sends id-like fields as bare integers sometimes. ``bool`` is excluded explicitly
    (``isinstance(True, int)`` is True in Python) so a stray ``True`` never becomes ``"True"``.
    """
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return str(candidate)
    return None


def _first_bool(*candidates: Any) -> bool | None:
    """Strictly a JSON boolean: ``1`` / ``"true"`` do NOT count as True (they mean "absent")."""
    for candidate in candidates:
        if isinstance(candidate, bool):
            return candidate
    return None


def parse_event_id(body: dict[str, Any]) -> str | None:
    """The per-event id: ``profile_event_id`` FIRST (this is the bug that ignored every event)."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        body.get("profile_event_id"),
        props.get("profile_event_id"),
        body.get("event_id"),
        body.get("id"),
    )


def parse_event_type(body: dict[str, Any]) -> str:
    props = _as_dict(body.get("event_properties"))
    raw = _first_str(
        body.get("event_type"),
        body.get("event"),
        props.get("event_type"),
        body.get("type"),
    )
    return raw.lower() if raw is not None else ""


def parse_customer_user_id(body: dict[str, Any]) -> str | None:
    """The customer identifier — kept as a STRING, deliberately.

    Adapty sends the id the client gave it, which in practice is a **deviceId**. It is resolved
    against ``auth_devices`` by the shared ``resolve_user()``; parsing it as a UUID here would throw
    away identifiers that are perfectly resolvable.
    """
    profile = _as_dict(body.get("profile"))
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        body.get("customer_user_id"),
        profile.get("customer_user_id"),
        props.get("customer_user_id"),
        body.get("user_id"),
    )


def parse_vendor_product_id(body: dict[str, Any]) -> str | None:
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        props.get("vendor_product_id"),
        props.get("product_id"),
        body.get("vendor_product_id"),
        body.get("product_id"),
    )


def parse_expires_at(body: dict[str, Any]) -> datetime.datetime | None:
    """ISO-8601 → aware datetime. Unparseable → ``None`` (the event is still applied)."""
    props = _as_dict(body.get("event_properties"))
    profile = _as_dict(body.get("profile"))
    raw = _first_str(
        props.get("subscription_expires_at"),
        props.get("expires_at"),
        body.get("subscription_expires_at"),
        body.get("expires_at"),
        profile.get("expires_at"),
    )
    if raw is None:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def parse_transaction_id(body: dict[str, Any]) -> str | None:
    """The Apple ``transaction_id`` — unique PER BILLING PERIOD, hence the grant key.

    NOT ``original_transaction_id``: that one is constant across the whole subscription chain, so
    renewals would reuse the first purchase's key and credit NOTHING, forever.
    """
    props = _as_dict(body.get("event_properties"))
    return _first_str(props.get("transaction_id"), body.get("transaction_id"))


def parse_original_transaction_id(body: dict[str, Any]) -> str | None:
    props = _as_dict(body.get("event_properties"))
    return _first_str(props.get("original_transaction_id"), body.get("original_transaction_id"))


def parse_is_active(body: dict[str, Any]) -> bool | None:
    props = _as_dict(body.get("event_properties"))
    return _first_bool(props.get("is_active"), body.get("is_active"))


def parse_access_level_id(body: dict[str, Any]) -> str | None:
    props = _as_dict(body.get("event_properties"))
    return _first_str(props.get("access_level_id"), body.get("access_level_id"))


def parse_will_renew(body: dict[str, Any]) -> bool | None:
    props = _as_dict(body.get("event_properties"))
    return _first_bool(props.get("will_renew"), body.get("will_renew"))


def classify_event(event: ParsedEvent) -> str:
    """GRANTING | EXPIRING | NOOP | TOKENS | REFUND.

    ``access_level_updated`` is conditional: premium+active → granting, inactive → expiring,
    anything else → **noop** (do NOT revoke on an ambiguous signal — the user may have paid).
    """
    if event.event_type in REFUND_EVENTS:
        return SEM_REFUND
    if event.event_type in TOKEN_EVENTS:
        return SEM_TOKENS
    if event.event_type in GRANTING_EVENTS:
        return SEM_GRANTING
    if event.event_type in EXPIRING_EVENTS:
        return SEM_EXPIRING
    if event.event_type in NOOP_EVENTS:
        # Auto-renew turned off: the user keeps the period he paid for. Revoking here would rob him.
        return SEM_NOOP
    if event.is_active is True and event.access_level_id == ACCESS_LEVEL_PREMIUM:
        return SEM_GRANTING
    if event.is_active is False:
        return SEM_EXPIRING
    return SEM_NOOP
