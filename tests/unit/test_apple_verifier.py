"""Apple identity-token verification — fail-closed on EVERY branch, 401 (never 500).

The JWKS client is the external boundary and is faked. Each failure mode of that boundary is
exercised SEPARATELY: they are different `except` clauses, and one of them missing means an
unverifiable token turns into a 500 — an availability signal to an attacker instead of a flat
rejection (or, worse, a pass).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.apple import AppleIdentityVerifier
from app.config import get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError
from tests.conftest import APPLE_AUDIENCE, APPLE_ISSUER, APPLE_TEST_SECRET, apple_identity_token

_APPLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _verifier(monkeypatch: pytest.MonkeyPatch, **env: str) -> AppleIdentityVerifier:
    """Fresh verifier built from a fresh settings object (get_settings is lru_cache'd)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    verifier = AppleIdentityVerifier()
    monkeypatch.setattr(
        "app.auth.apple.get_settings", get_settings
    )  # keep the module reading the same cache
    return verifier


@pytest.fixture(autouse=True)
def _restore_settings() -> Any:
    yield
    get_settings.cache_clear()


class _FakeJwks:
    """Stand-in for PyJWKClient (Apple's JWKS endpoint — the external boundary)."""

    def __init__(self, key: Any = None, error: Exception | None = None) -> None:
        self._key = key
        self._error = error

    def get_signing_key_from_jwt(self, token: str) -> Any:
        if self._error is not None:
            raise self._error
        return type("K", (), {"key": self._key})()


def _real_apple_token(**overrides: Any) -> str:
    claims: dict[str, Any] = {
        "sub": "apple-sub-1",
        "iss": APPLE_ISSUER,
        "aud": APPLE_AUDIENCE,
        "iat": 1_700_000_000,
        "exp": 4_000_000_000,
        "email": "a@example.com",
    }
    claims.update(overrides)
    return pyjwt.encode(claims, _APPLE_KEY, algorithm="RS256")


# --- the real (RS256) path -------------------------------------------------------------------
def test_valid_rs256_token_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch)
    monkeypatch.setattr(verifier, "_jwks_client", _FakeJwks(key=_APPLE_KEY.public_key()))
    identity = verifier.verify(_real_apple_token(), None)
    assert identity.apple_sub == "apple-sub-1"
    assert identity.email == "a@example.com"


@pytest.mark.parametrize(
    "override",
    [
        {"iss": "https://evil.example.com"},
        {"aud": "com.attacker.app"},
        {"exp": 1_600_000_000},  # expired
    ],
    ids=["wrong_iss", "wrong_aud", "expired"],
)
def test_invalid_claims_are_401(monkeypatch: pytest.MonkeyPatch, override: dict[str, Any]) -> None:
    verifier = _verifier(monkeypatch)
    monkeypatch.setattr(verifier, "_jwks_client", _FakeJwks(key=_APPLE_KEY.public_key()))
    with pytest.raises(UnauthorizedError):
        verifier.verify(_real_apple_token(**override), None)


def test_signature_by_a_foreign_key_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(monkeypatch)
    # We resolve APPLE's key, but the token was signed by someone else.
    monkeypatch.setattr(verifier, "_jwks_client", _FakeJwks(key=_APPLE_KEY.public_key()))
    forged = pyjwt.encode(
        {
            "sub": "x",
            "iss": APPLE_ISSUER,
            "aud": APPLE_AUDIENCE,
            "exp": 4_000_000_000,
        },
        other,
        algorithm="RS256",
    )
    with pytest.raises(UnauthorizedError):
        verifier.verify(forged, None)


def test_missing_required_claim_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch)
    monkeypatch.setattr(verifier, "_jwks_client", _FakeJwks(key=_APPLE_KEY.public_key()))
    no_sub = pyjwt.encode(
        {"iss": APPLE_ISSUER, "aud": APPLE_AUDIENCE, "exp": 4_000_000_000},
        _APPLE_KEY,
        algorithm="RS256",
    )
    with pytest.raises(UnauthorizedError):
        verifier.verify(no_sub, None)


# --- FAIL-CLOSED, ONE BRANCH AT A TIME (each is a separate `except` in the verifier) ----------
@pytest.mark.parametrize(
    "error",
    [
        pyjwt.PyJWKClientError("unknown kid"),
        pyjwt.PyJWKSetError("JWKS document has no keys"),
        pyjwt.PyJWKError("unusable key material"),
        pyjwt.InvalidTokenError("malformed JWS header"),
        json.JSONDecodeError("not json", "<doc>", 0),
        TimeoutError("jwks fetch timed out"),  # OSError subclass: network/timeout
        OSError("connection refused"),
    ],
    ids=[
        "unknown_kid",
        "empty_jwks",
        "bad_key_material",
        "unparsable_header",
        "jwks_not_json",
        "timeout",
        "network_error",
    ],
)
def test_every_jwks_failure_is_401_not_500(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    verifier = _verifier(monkeypatch)
    monkeypatch.setattr(verifier, "_jwks_client", _FakeJwks(error=error))
    with pytest.raises(UnauthorizedError):
        verifier.verify(_real_apple_token(), None)


# --- the HS256 test seam ---------------------------------------------------------------------
def test_hs256_outside_test_mode_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alg-confusion guard: the test seam is not an open door in prod (BR-AUTH-9)."""
    verifier = _verifier(monkeypatch, APPLE_TEST_MODE="false", APPLE_TEST_SECRET="")
    token = apple_identity_token(subject="s1")  # HS256, signed with the test secret
    with pytest.raises(UnauthorizedError):
        verifier.verify(token, None)


def test_hs256_inside_test_mode_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch, APPLE_TEST_MODE="true", APPLE_TEST_SECRET=APPLE_TEST_SECRET)
    identity = verifier.verify(apple_identity_token(subject="s1", email="s1@example.com"), None)
    assert identity.apple_sub == "s1"


def test_hs256_with_a_wrong_secret_is_401_inside_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch, APPLE_TEST_MODE="true", APPLE_TEST_SECRET=APPLE_TEST_SECRET)
    token = apple_identity_token(subject="s1", key="a-different-secret")
    with pytest.raises(UnauthorizedError):
        verifier.verify(token, None)


# --- configuration / nonce -------------------------------------------------------------------
def test_no_audience_configured_is_503_not_a_skipped_check(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch, APPLE_AUDIENCE="", APPSTORE_BUNDLE_ID="")
    assert verifier.configured is False
    with pytest.raises(ServiceUnavailableError):
        verifier.verify(apple_identity_token(subject="s1"), None)


def test_nonce_mismatch_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch, APPLE_TEST_MODE="true", APPLE_TEST_SECRET=APPLE_TEST_SECRET)
    token = apple_identity_token(subject="s1", nonce=hashlib.sha256(b"the-real-nonce").hexdigest())
    with pytest.raises(UnauthorizedError):
        verifier.verify(token, "another-nonce")


def test_matching_nonce_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch, APPLE_TEST_MODE="true", APPLE_TEST_SECRET=APPLE_TEST_SECRET)
    raw = "the-real-nonce"
    token = apple_identity_token(subject="s1", nonce=hashlib.sha256(raw.encode()).hexdigest())
    assert verifier.verify(token, raw).apple_sub == "s1"


def test_garbage_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _verifier(monkeypatch)
    with pytest.raises(UnauthorizedError):
        verifier.verify("not-a-jwt", None)
