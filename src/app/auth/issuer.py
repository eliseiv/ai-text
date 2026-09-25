"""``TokenIssuer`` — RS256 access-token signing for the embedded issuer.

Claims: ``sub`` = userId, ``device_id``, ``iss``, ``aud``, ``iat``, ``exp``; ``kid`` in the header.
Verified by the gateway's ``JwtVerifier`` from the SAME config — a self-consistent loop.

**Why RS256 and not HS256:** the public key can be handed out (``GET /v1/auth/jwks``) — anyone may
VERIFY a token, only the holder of the private key may ISSUE one. It also keeps the migration path
to an external IdP open.

No private key configured → the issuer is "unavailable" → ``/v1/auth/*`` returns **503**. Never
"issue without a signature" and never "generate a temporary key" (fail-closed, BR-AUTH-8). The
private key and the signed token are NEVER logged.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from jwt.algorithms import RSAAlgorithm

from app.config import CoreSettings


def build_jwks(public_key_pem: str, kid: str) -> dict[str, object]:
    """Build a JWKS document (one RSA public key) from a PEM public key. Public material only."""
    algorithm = RSAAlgorithm(RSAAlgorithm.SHA256)
    prepared = algorithm.prepare_key(public_key_pem)
    raw: dict[str, object] = json.loads(algorithm.to_jwk(prepared))
    # Emit exactly the contract fields. PyJWT also adds `key_ops`, which the strict
    # response schema would reject.
    key = {
        "kty": raw["kty"],
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": raw["n"],
        "e": raw["e"],
    }
    return {"keys": [key]}


class IssuerNotConfiguredError(Exception):
    """No private signing key — the caller maps this to 503."""


class TokenIssuer:
    """Signs RS256 access tokens for device-based identities."""

    def __init__(self, settings: CoreSettings) -> None:
        self._private_key = settings.resolve_private_key()
        self._issuer = settings.jwt_issuer or None
        self._audience = settings.jwt_audience or None
        self._kid = settings.jwt_kid or None
        self._access_ttl = settings.auth_access_ttl_seconds

    @property
    def configured(self) -> bool:
        """True iff a private signing key is available (otherwise the endpoints must 503)."""
        return bool(self._private_key)

    @property
    def access_ttl_seconds(self) -> int:
        return self._access_ttl

    def issue_access_token(self, *, user_id: uuid.UUID, device_id: str) -> str:
        """Sign an access JWT for (userId, deviceId). The token is a credential — never logged."""
        if not self._private_key:
            raise IssuerNotConfiguredError("no private signing key configured")
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "sub": str(user_id),  # userId is assigned by the BACKEND, never taken from the body
            "device_id": device_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self._access_ttl)).timestamp()),
        }
        if self._issuer is not None:
            claims["iss"] = self._issuer
        if self._audience is not None:
            claims["aud"] = self._audience
        headers = {"kid": self._kid} if self._kid is not None else None
        return jwt.encode(claims, self._private_key, algorithm="RS256", headers=headers)
