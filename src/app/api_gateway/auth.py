"""JWT authentication (RS256) via JWKS or a static public key.

Verifies signature, ``exp``, ``iss``, ``aud``; extracts ``sub`` (userId) and ``device_id``. The
token is never logged. JWKS keys are cached for a short TTL.

Also hosts ``require_admin``: admin authorization is fully separate from
``get_current_user`` — different secret, different header, different dependency, and NO lazy
provisioning (an admin is not a user of the system).
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from typing import Annotated

import httpx
import jwt
from fastapi import Depends
from jwt import PyJWKClient

from app.api_gateway.openapi_security import admin_scheme
from app.config import get_settings
from app.errors import UnauthorizedError


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: uuid.UUID
    device_id: str | None


class JwtVerifier:
    def __init__(self) -> None:
        settings = get_settings()
        self._issuer = settings.jwt_issuer or None
        self._audience = settings.jwt_audience or None
        self._jwks_url = settings.jwt_jwks_url or None
        # File-path priority over the \n-escaped string.
        self._public_key = settings.resolve_public_key() or None
        # PyJWKClient keeps a per-kid cache, so rotation / multiple kids each resolve their own
        # signing key; `lifespan` bounds how long a fetched JWKS is reused.
        self._jwks_client: PyJWKClient | None = (
            PyJWKClient(
                self._jwks_url,
                cache_keys=True,
                lifespan=settings.jwks_cache_ttl_seconds,
            )
            if self._jwks_url
            else None
        )

    def _signing_key(self, token: str) -> object:
        if self._jwks_client is not None:
            try:
                return self._jwks_client.get_signing_key_from_jwt(token).key
            except (jwt.PyJWKClientError, httpx.HTTPError) as exc:
                raise UnauthorizedError("unable to resolve signing key") from exc
        if self._public_key:
            return self._public_key
        raise UnauthorizedError("no JWT verification key configured")

    def verify(self, token: str) -> AuthenticatedUser:
        key = self._signing_key(token)
        options = {"require": ["exp", "sub"], "verify_aud": self._audience is not None}
        try:
            claims = jwt.decode(
                token,
                key=key,  # type: ignore[arg-type]
                algorithms=["RS256"],  # алгоритм фиксирован: никакой alg-confusion
                audience=self._audience,
                issuer=self._issuer,
                options=options,
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid token") from exc

        sub = claims.get("sub")
        if not sub:
            raise UnauthorizedError("missing sub")
        try:
            user_id = uuid.UUID(str(sub))
        except ValueError as exc:
            raise UnauthorizedError("sub is not a valid user id") from exc
        return AuthenticatedUser(user_id=user_id, device_id=claims.get("device_id"))


_verifier_singleton: JwtVerifier | None = None


def get_jwt_verifier() -> JwtVerifier:
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = JwtVerifier()
    return _verifier_singleton


def _admin_token_matches(presented: str) -> bool:
    """Constant-time compare against the active admin secret(s).

    Matches ``ADMIN_API_SECRET`` or (during rotation) ``ADMIN_API_SECRET_PREV``. An empty/unset
    configured secret never matches, so a blank header can never authenticate. Both candidates are
    always evaluated to avoid an early-exit timing leak.
    """
    settings = get_settings()
    matched = False
    for candidate in (settings.admin_api_secret, settings.admin_api_secret_prev):
        if candidate and hmac.compare_digest(presented, candidate):
            matched = True
    return matched


async def require_admin(
    x_admin_token: Annotated[str | None, Depends(admin_scheme)] = None,
) -> None:
    """Authorize an admin request via the isolated ``X-Admin-Token``.

    No JWT, no lazy provisioning, no ``users`` row for the actor: the admin has no
    ``sub`` and is recorded as ``admin`` in audit. A missing/mismatching token raises 401 without
    revealing which. The secret is never logged (redaction covers ``X-Admin-Token``).
    """
    if x_admin_token is None or not _admin_token_matches(x_admin_token):
        raise UnauthorizedError("invalid admin token")
