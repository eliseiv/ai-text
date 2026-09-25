"""Apple identity-token verification — Sign in with Apple.

Branch selection is by the JWS header ``alg``:

* ``RS256`` — ALWAYS the real path: the signing key is resolved from Apple's JWKS (cached) and the
  signature / ``iss`` / ``aud`` / ``exp`` and the required claims are verified;
* ``HS256`` — the TEST branch, honoured ONLY when ``APPLE_TEST_MODE`` **and** a non-empty
  ``APPLE_TEST_SECRET`` are both set. HS256 outside test-mode → ``401`` (no alg-confusion:
  the test seam is not an open door in prod, BR-AUTH-9).

**There is no branch in which an invalid token passes.** Any failure — bad signature, wrong
``iss``/``aud``, expired, missing claim, unresolvable JWKS key, network error, nonce mismatch —
raises ``UnauthorizedError`` (401, fail-closed) with a generic message: the reason is never
disclosed to the caller. No audience configured → ``503`` ("not configured"), NEVER "skip the
``aud`` check".

The identity token and the nonce are never logged and never embedded in exception text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

from app.config import get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError

# Hard cap on the BLOCKING JWKS fetch. PyJWKClient uses urllib, whose default timeout is ~30 s —
# on a public endpoint that is a 30-second stall per cache-miss. The verification runs off the
# event loop (``AuthService.sign_in_with_apple`` → ``to_thread``), but a worker thread held for
# 30 s is still a scarce resource. Apple's JWKS is a small static document; 5 s is generous.
_JWKS_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class VerifiedAppleIdentity:
    """Result of a successful verification. The identity KEY is ``sub``, never ``email``.

    Apple returns ``email`` only on the first consent, allows hiding it behind a private relay and
    it may change. ``sub`` is stable forever (BR-AUTH-7).
    """

    apple_sub: str
    email: str | None
    email_verified: bool


class AppleIdentityVerifier:
    """Verifies Apple-signed OIDC identity tokens (native Sign in with Apple flow)."""

    def __init__(self) -> None:
        settings = get_settings()
        self._issuer = settings.apple_oidc_issuer
        self._audience = settings.apple_audience_resolved()  # = the app bundle id
        # test-mode is active ONLY when the flag AND the secret are both present; it never weakens
        # the real RS256 path.
        self._test_secret = settings.apple_test_secret
        self._test_mode = settings.apple_test_mode and bool(self._test_secret)
        # PyJWKClient keeps a per-kid cache; `lifespan` bounds how long a fetched JWKS is reused.
        # `timeout` is NOT optional here — see _JWKS_TIMEOUT_SECONDS.
        self._jwks_client = PyJWKClient(
            settings.apple_jwks_url,
            cache_keys=True,
            lifespan=settings.jwks_cache_ttl_seconds,
            timeout=_JWKS_TIMEOUT_SECONDS,
        )

    @property
    def configured(self) -> bool:
        """True iff the Apple audience is configured (otherwise the endpoint must return 503)."""
        return bool(self._audience)

    def verify(self, identity_token: str, nonce: str | None) -> VerifiedAppleIdentity:
        if not self._audience:
            # Operational mis-configuration, not a client error.
            raise ServiceUnavailableError("apple sign-in is not configured")

        try:
            header = jwt.get_unverified_header(identity_token)
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid apple identity token") from exc
        alg = str(header.get("alg", ""))

        if alg == "HS256":
            if not self._test_mode:
                # Fail-closed: HS256 is NEVER accepted outside test-mode.
                raise UnauthorizedError("invalid apple identity token")
            claims = self._decode_test(identity_token)
        else:
            claims = self._decode_real(identity_token)

        self._check_nonce(claims, nonce)

        sub: Any = claims.get("sub")
        if not sub:
            raise UnauthorizedError("invalid apple identity token")
        return VerifiedAppleIdentity(
            apple_sub=str(sub),
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified", False)),
        )

    def _decode_real(self, identity_token: str) -> dict[str, Any]:
        """Real Apple RS256 token: JWKS signing key + signature/iss/aud/exp verification."""
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(identity_token).key
        except (
            jwt.PyJWKClientError,  # unknown kid / no usable key
            jwt.PyJWKSetError,  # JWKS document without keys
            jwt.PyJWKError,  # unusable key material in the document
            jwt.InvalidTokenError,  # unparsable header (no kid, malformed JWS)
            json.JSONDecodeError,  # non-JSON body served at the JWKS URL
            OSError,  # network / timeout (urllib.error.URLError is an OSError)
        ) as exc:
            # EVERY failure to resolve the key is fail-closed 401 — never a 5xx and never a pass.
            # An unverifiable token must not become valid merely because Apple was unreachable or
            # answered garbage; and an unhandled exception here would surface as 500, which would
            # be an availability signal to an attacker instead of a flat rejection.
            raise UnauthorizedError("invalid apple identity token") from exc
        try:
            claims: dict[str, Any] = jwt.decode(
                identity_token,
                key=signing_key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["sub", "iss", "aud", "exp"], "verify_aud": True},
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid apple identity token") from exc
        return claims

    def _decode_test(self, identity_token: str) -> dict[str, Any]:
        """test-mode: HS256 token signed with ``APPLE_TEST_SECRET`` (hermetic tests, no Apple).

        Identical response semantics to the real path: a bad signature / wrong iss / wrong aud /
        expired token raises the same 401 as a forged real token.
        """
        try:
            claims: dict[str, Any] = jwt.decode(
                identity_token,
                key=self._test_secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["sub", "iss", "aud", "exp"], "verify_aud": True},
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid apple identity token") from exc
        return claims

    @staticmethod
    def _check_nonce(claims: dict[str, Any], nonce: str | None) -> None:
        """Optional nonce check: verified only when BOTH sides are present.

        Apple stores ``sha256(raw_nonce)`` (hex) in the ``nonce`` claim. Mismatch → 401. Plain
        string comparison is fine — the compared values are hashes, not secrets.
        """
        claim_nonce = claims.get("nonce")
        if claim_nonce and nonce:
            expected = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
            if expected != str(claim_nonce):
                raise UnauthorizedError("invalid apple identity token")


_verifier_singleton: AppleIdentityVerifier | None = None


def get_apple_verifier() -> AppleIdentityVerifier:
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = AppleIdentityVerifier()
    return _verifier_singleton
