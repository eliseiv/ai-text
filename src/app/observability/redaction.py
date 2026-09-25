"""Secret redaction for logs and audit payloads.

Used by BOTH the logging formatter and ``AuditService`` — so a secret cannot leak through either
path, and ``assert_no_secrets()`` is the last guard before an ``audit_logs.payload`` row is
persisted forever.

**Domain-neutral by construction:** every rule keys off the SHAPE of a secret (the field NAME),
never off a domain concept. A rule about "attachments" or "chat messages" would put domain
knowledge into the core; a domain that needs more redaction passes its own allowlist
projection into the audit payload instead.

The denylist is defined literally: ``Authorization`` ·
``X-Admin-Token`` · ``*key*`` · ``*token*`` · ``*secret*`` · ``*password*`` · ``nonce`` ·
``customerEmail`` (and any other e-mail) · card fields (``CardFirstSix`` / ``CardLastFour`` /
``Issuer`` / ``CardType``) · raw provider payloads (``transaction`` / ``jws`` / ``receipt``) ·
inline base64 blobs (``data``).
"""

from __future__ import annotations

from typing import Any

REDACTED = "***REDACTED***"

# Substrings (lowercased) that mark a value as sensitive wherever they appear in the key.
# `card` covers CardFirstSix / CardLastFour / CardType; `email` covers customerEmail and any other
# address (PII is forbidden in audit payloads).
_DENY_SUBSTRINGS = (
    "key",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "card",
    "email",
)

# Exact field names (lowercased) carrying raw secrets, PII or provider payloads. `nonce` is an
# encryption nonce (it was in the source denylist — losing it here was a regression). `issuer` is
# a CloudPayments card field. `data` is an inline base64 blob (file/attachment content): a generic
# key-name rule, not a domain concept.
_DENY_EXACT = (
    "apikey",
    "transaction",
    "jws",
    "receipt",
    "nonce",
    "dek",
    "issuer",
    "data",
    "x-admin-token",
    "x_admin_token",
)


# Exact field names (lowercased) that survive redaction even though a denylist substring matches
# them. Keep this list SHORT and justified: every entry is a documented, adjudicated non-secret.
_ALLOW_EXACT = (
    "idempotencykey",
    "idempotency_key",
    "grantidempotencykey",
    "grant_idempotency_key",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    # ALLOWLIST — checked BEFORE the substring denylist, because these fields legitimately contain
    # the substring `key` but are NOT secrets:
    #   * idempotencyKey / grantIdempotencyKey — the ledger idempotency key. It is REQUIRED in the
    #     `admin_grant` / `admin_subscription_grant` audit events and is the ONLY attribution
    #     thread of an admin grant ("who credited, under which ticket": `support-ticket-1234`) —
    #     adjudicated as non-secret; the AC test asserts its presence in `audit_logs`.
    #     Redacting it silently destroys the attribution of every manual credit grant.
    if lowered in _ALLOW_EXACT:
        return False
    # Status-like metadata (`keyStatus`, `paymentStatuses`) is non-sensitive and MUST survive:
    # the outcome logs are built on it, and no raw secret ever lives in such a field.
    if lowered.endswith("status") or lowered.endswith("statuses"):
        return False
    if lowered in _DENY_EXACT:
        return True
    return any(sub in lowered for sub in _DENY_SUBSTRINGS)


def redact(value: Any) -> Any:
    """Recursively redact sensitive values in dicts/lists. Returns a redacted copy."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_sensitive_key(k):
                result[k] = REDACTED
            else:
                result[k] = redact(v)
        return result
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def assert_no_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Redaction guard for audit payloads. Returns a redacted copy (defensive)."""
    return redact(payload)  # type: ignore[no-any-return]
