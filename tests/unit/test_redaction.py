"""Redaction — allowlist AND denylist, both directions."""

from __future__ import annotations

import pytest

from app.observability.redaction import _ALLOW_EXACT, REDACTED, redact

# NEGATIVE: the substring rule must stay alive. Each of these has leaked from a real service.
_SECRET_KEYS = [
    "apiKey",
    "jwt_private_key",
    "stripe_api_key",
    "webhook_secret",
    "X-Admin-Token",
    "Authorization",
    "nonce",
    "customerEmail",
    "CardLastFour",
    "CardFirstSix",
    "CardType",
    "Issuer",
    "password",
    "refreshToken",
    "accessToken",
    "transaction",
    "jws",
    "receipt",
    "data",
    "credential",
]


@pytest.mark.parametrize("key", _SECRET_KEYS)
def test_sensitive_fields_are_redacted(key: str) -> None:
    assert redact({key: "s3cret"}) == {key: REDACTED}


# POSITIVE: the allowlist. `idempotencyKey` is the ONLY attribution thread of an admin grant
# ("who credited, under which support ticket") — redacting it destroys it.
@pytest.mark.parametrize(
    "key", ["idempotencyKey", "idempotency_key", "grant_idempotency_key", "grantIdempotencyKey"]
)
def test_idempotency_keys_survive_verbatim(key: str) -> None:
    assert redact({key: "support-ticket-1234"}) == {key: "support-ticket-1234"}


def test_allowlist_does_not_creep() -> None:
    """The allowlist is EXACTLY the adjudicated names.

    A fifth entry added without updating the security doc fails here — an allowlist that grows
    quietly is how a secret ends up in the log.
    """
    assert set(_ALLOW_EXACT) == {
        "idempotencykey",
        "idempotency_key",
        "grantidempotencykey",
        "grant_idempotency_key",
    }


def test_allowlist_is_exact_match_not_substring() -> None:
    # A field merely CONTAINING an allowlisted name is still redacted.
    assert redact({"my_idempotencyKey_secret": "x"}) == {"my_idempotencyKey_secret": REDACTED}


def test_status_like_fields_survive() -> None:
    # Outcome logs are built on them and they carry no secret.
    assert redact({"keyStatus": "valid", "paymentStatuses": ["succeeded"]}) == {
        "keyStatus": "valid",
        "paymentStatuses": ["succeeded"],
    }


def test_redaction_is_recursive_over_dicts_and_lists() -> None:
    payload = {
        "level1": {"apiKey": "x", "safe": 1},
        "items": [{"authorization": "Bearer y"}, {"idempotencyKey": "k"}],
    }
    assert redact(payload) == {
        "level1": {"apiKey": REDACTED, "safe": 1},
        "items": [{"authorization": REDACTED}, {"idempotencyKey": "k"}],
    }


def test_non_dict_values_pass_through() -> None:
    assert redact("plain") == "plain"
    assert redact(42) == 42
    assert redact(None) is None
