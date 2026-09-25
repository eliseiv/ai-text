"""Defensive parsing of a CloudPayments-format callback.

The aggregator posts flat PascalCase fields plus a ``Data`` field that is itself a **JSON string**
(not an object). Nothing here raises: a malformed callback becomes an ``ignored`` outcome, never a
422 the aggregator would retry forever.

**Card PII is excluded BY CONSTRUCTION.** ``CardFirstSix`` / ``CardLastFour`` / ``Issuer`` /
``CardType`` are never read into any dataclass — not "redacted later", simply never taken. Nothing
downstream (logs, audit, ``payments.payload``) can leak what was never parsed.

Note what the callback does NOT decide: ``TransactionId`` is log context only. It is not the money
key and not the dedup key — we do not trust the callback, so its identifier cannot key money
(otherwise an attacker "occupying" a TransactionId could block a real grant).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

KIND_SUBSCRIPTION = "subscription"
KIND_TOKENS = "tokens"

# Authoritative product classes as returned by the aggregator's verify API: the class
# comes from `product.payment_type`, NOT from a name heuristic (fragile and bypassable).
PAYMENT_TYPE_ONE_TIME = "one_time"
PAYMENT_TYPE_SUBSCRIPTION = "subscription"


@dataclass(frozen=True)
class ParsedCallback:
    """Only the trigger fields. No card data, no amounts used for money, no raw body."""

    device_id: str  # `AccountId` — arrives UPPERCASE; usually a deviceId, not our userId
    transaction_id: str | None  # log context ONLY — never a key
    status: str
    operation_type: str


def _first_str(*candidates: Any) -> str | None:
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return str(candidate)
    return None


def parse_data(body: dict[str, Any]) -> dict[str, Any]:
    """``Data`` is a JSON *string* in the real payload (an object only sometimes). Never raises."""
    raw = body.get("Data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def parse_status(body: dict[str, Any]) -> str:
    return str(body.get("Status") or "").strip().lower()


def parse_operation_type(body: dict[str, Any]) -> str:
    return str(body.get("OperationType") or "").strip().lower()


def parse_gate(status: str, operation_type: str) -> bool:
    """A COMPLETED payment or a REFUND is a trigger (both are then confirmed through verify).
    Anything else moved no money."""
    if operation_type == "refund":
        return True
    return status == "completed" and operation_type == "payment"


def parse_transaction_id(body: dict[str, Any]) -> str | None:
    """Log context only. Deliberately NOT used as a dedup or idempotency key."""
    return _first_str(body.get("TransactionId"))


def parse_device_id(body: dict[str, Any], data: dict[str, Any]) -> str | None:
    """``AccountId`` (top-level) → fallback ``Data.user_id``.

    Kept as a STRING and resolved through the shared ``resolve_user()`` (case-insensitively): the
    aggregator forwards the identifier in UPPERCASE while our stored ``device_id`` may be either.
    """
    return _first_str(body.get("AccountId"), data.get("user_id"))


def is_uuid(value: str) -> bool:
    """SSRF guard: only a canonical UUID may be interpolated into the outgoing verify path."""
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def kind_for_payment_type(payment_type: str) -> str | None:
    """``one_time`` → tokens, ``subscription`` → subscription. Anything else → ``None`` (skip)."""
    if payment_type == PAYMENT_TYPE_ONE_TIME:
        return KIND_TOKENS
    if payment_type == PAYMENT_TYPE_SUBSCRIPTION:
        return KIND_SUBSCRIPTION
    return None
