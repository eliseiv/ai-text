"""``AuthService`` — device-based identity + Sign in with Apple.

Two identity levels:

* **device-based** — zero registration friction (a signup screen before first use kills
  conversion): ``deviceId`` → ``userId``, find-or-create;
* **Sign in with Apple** — cross-device, survives a reinstall: ``apple_sub`` → ``userId``, added
  ON TOP of the device identity so an anonymous account can be UPGRADED without losing its
  credits, subscription and history.

Access token = RS256 JWT (stateless, 1 h). Refresh = OPAQUE random bytes (30 d), stored ONLY as
``sha256`` — a database dump therefore yields no usable refresh tokens. Opaque (not a JWT) because
a JWT cannot be revoked without a revocation list, i.e. without making it stateful anyway.

**Reuse-detect is fail-safe, and deliberately harsh:** a refresh token presented twice is either a
client retry or a THEFT, and the two are indistinguishable — so the whole device chain is revoked.
The cost of a false positive is low (device-based re-auth is transparent: ``register`` with the
same ``deviceId`` returns the SAME ``userId``, data intact), while a stolen refresh token is
actually DETECTED rather than merely time-limited.

``auth_devices`` is part of the PAYMENT circuit: payment providers put the **deviceId** in their
"user" field, and the webhook resolve reads it from this table.

Tokens (access, refresh, Apple identity token) and nonces are NEVER logged.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    AUTH_ACTION_APPLE_IDENTITY_LINKED,
    AUTH_ACTION_REFRESH_CHAIN_REVOKED,
    EVENT_AUTH_EVENT,
    AuditService,
)
from app.auth.apple import AppleIdentityVerifier
from app.auth.issuer import IssuerNotConfiguredError, TokenIssuer
from app.config import CoreSettings
from app.errors import ServiceUnavailableError, UnauthorizedError


@dataclass(frozen=True)
class IssuedTokens:
    user_id: uuid.UUID
    device_id: str
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


def _hash_refresh(token: str) -> str:
    """sha256 hex of the opaque refresh token. The plaintext is never persisted or logged."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        issuer: TokenIssuer,
        settings: CoreSettings,
        apple_verifier: AppleIdentityVerifier,
        audit: AuditService,
    ) -> None:
        self._session = session
        self._issuer = issuer
        self._refresh_ttl = settings.auth_refresh_ttl_seconds
        self._apple = apple_verifier
        self._audit = audit

    def _require_issuer(self) -> None:
        """No private key → 503. Never "issue without a signature" (BR-AUTH-8)."""
        if not self._issuer.configured:
            raise ServiceUnavailableError("auth issuer is not configured")

    async def _find_or_create_identity(self, device_id: str) -> uuid.UUID:
        """Resolve the userId of a device, creating ``users`` + ``auth_devices`` when new.

        Race-safe: both inserts use ``ON CONFLICT DO NOTHING`` and the winning row is re-read, so
        two concurrent ``register`` calls for one deviceId converge on ONE userId (BR-AUTH-1).
        """
        existing = await self._session.execute(
            text("SELECT user_id FROM auth_devices WHERE device_id = :device_id"),
            {"device_id": device_id},
        )
        row = existing.first()
        if row is not None:
            await self._session.execute(
                text("UPDATE auth_devices SET last_seen_at = now() WHERE device_id = :device_id"),
                {"device_id": device_id},
            )
            return uuid.UUID(str(row[0]))

        new_user_id = uuid.uuid4()  # the backend assigns userId — never the client (BR-AUTH-2)
        # Eager provisioning; the gateway's lazy upsert remains the fallback — the two
        # are the same idempotent statement, so they cannot disagree.
        await self._session.execute(
            text("INSERT INTO users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"),
            {"id": str(new_user_id)},
        )
        await self._session.execute(
            text(
                "INSERT INTO auth_devices (user_id, device_id) VALUES (:user_id, :device_id) "
                "ON CONFLICT (device_id) DO NOTHING"
            ),
            {"user_id": str(new_user_id), "device_id": device_id},
        )
        resolved = await self._session.execute(
            text("SELECT user_id FROM auth_devices WHERE device_id = :device_id"),
            {"device_id": device_id},
        )
        winner = resolved.first()
        if winner is None:  # pragma: no cover - the insert above guarantees a row
            raise ServiceUnavailableError("failed to provision device identity")
        return uuid.UUID(str(winner[0]))

    async def _issue_pair(self, user_id: uuid.UUID, device_id: str) -> IssuedTokens:
        try:
            access_token = self._issuer.issue_access_token(user_id=user_id, device_id=device_id)
        except IssuerNotConfiguredError as exc:
            raise ServiceUnavailableError("auth issuer is not configured") from exc

        refresh_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._refresh_ttl)
        await self._session.execute(
            text(
                "INSERT INTO auth_refresh_tokens (user_id, device_id, token_hash, expires_at) "
                "VALUES (:user_id, :device_id, :token_hash, :expires_at)"
            ),
            {
                "user_id": str(user_id),
                "device_id": device_id,
                "token_hash": _hash_refresh(refresh_token),  # ONLY the hash reaches the DB
                "expires_at": expires_at,
            },
        )
        return IssuedTokens(
            user_id=user_id,
            device_id=device_id,
            access_token=access_token,
            expires_in=self._issuer.access_ttl_seconds,
            refresh_token=refresh_token,
            refresh_expires_in=self._refresh_ttl,
        )

    async def register_or_token(self, device_id: str | None) -> IssuedTokens:
        """find-or-create the device identity and issue a pair (``/register`` and ``/token``).

        ``deviceId`` is optional: absent → a UUIDv4 is generated and returned to the client. The
        two endpoints differ only in whether the schema requires ``deviceId``.
        """
        self._require_issuer()
        resolved_device_id = device_id or str(uuid.uuid4())
        user_id = await self._find_or_create_identity(resolved_device_id)
        tokens = await self._issue_pair(user_id, resolved_device_id)
        await self._session.commit()
        return tokens

    async def sign_in_with_apple(
        self, identity_token: str, device_id: str | None, nonce: str | None
    ) -> IssuedTokens:
        """Verify the Apple identity token and issue OUR pair.

        The Apple token NEVER becomes an access token (BR-AUTH-6) — it is exchanged for ours.
        """
        self._require_issuer()
        # The verifier owns both the "not configured" → 503 and the "bad token" → 401 decisions.
        #
        # ⚠ RUN IT OFF THE EVENT LOOP. `verify()` is synchronous and a JWKS cache-miss fetches from
        # Apple with a BLOCKING urllib call. Calling it inline would stall the entire worker — every
        # request of every user — for the duration of that fetch. And this endpoint is PUBLIC (no
        # JWT): a stream of tokens with unknown `kid` forces a fresh fetch each time (PyJWKClient
        # does not cache failures), so an inline call is a trivial one-request-per-stall DoS. The
        # per-IP limiter is not a mitigation: it is distributed-bypassable and fails OPEN when Redis
        # is down.
        identity = await to_thread.run_sync(self._apple.verify, identity_token, nonce)
        resolved_device_id = device_id or str(uuid.uuid4())

        target_user_id = await self._resolve_apple_user(
            apple_sub=identity.apple_sub, email=identity.email, device_id=resolved_device_id
        )
        await self._upsert_device(resolved_device_id, target_user_id)

        tokens = await self._issue_pair(target_user_id, resolved_device_id)
        await self._session.commit()
        return tokens

    async def _resolve_apple_user(
        self, *, apple_sub: str, email: str | None, device_id: str
    ) -> uuid.UUID:
        """Map an Apple identity to a userId, linking or creating as needed.

        | situation | result |
        |---|---|
        | ``apple_sub`` known | the SAME userId (cross-device) |
        | ``apple_sub`` new, the device user has NO Apple identity | LINK to it — credits,
          subscription and history are preserved (the anonymous-account upgrade: the whole reason
          Apple sits ON TOP of device identity rather than replacing it) |
        | ``apple_sub`` new, the device user ALREADY has an Apple identity | create a NEW user |

        Race-safe: the identity INSERT is ``ON CONFLICT (provider, subject) DO NOTHING`` + re-read,
        so a concurrent first sign-in of one ``apple_sub`` converges on a single userId. ``email``
        is written only when the row is created and is never overwritten (Apple sends it only on
        the first consent).
        """
        existing = await self._session.execute(
            text(
                "SELECT user_id FROM auth_identities "
                "WHERE provider = 'apple' AND subject = :subject"
            ),
            {"subject": apple_sub},
        )
        row = existing.first()
        if row is not None:
            return uuid.UUID(str(row[0]))

        device_user_id = await self._find_or_create_identity(device_id)

        has_apple = await self._session.execute(
            text(
                "SELECT 1 FROM auth_identities "
                "WHERE user_id = :user_id AND provider = 'apple' LIMIT 1"
            ),
            {"user_id": str(device_user_id)},
        )
        if has_apple.first() is None:
            link_to = device_user_id  # upgrade the anonymous account in place
        else:
            link_to = uuid.uuid4()
            await self._session.execute(
                text("INSERT INTO users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"),
                {"id": str(link_to)},
            )

        await self._session.execute(
            text(
                "INSERT INTO auth_identities (user_id, provider, subject, email) "
                "VALUES (:user_id, 'apple', :subject, :email) "
                "ON CONFLICT (provider, subject) DO NOTHING"
            ),
            {"user_id": str(link_to), "subject": apple_sub, "email": email},
        )
        resolved = await self._session.execute(
            text(
                "SELECT user_id FROM auth_identities "
                "WHERE provider = 'apple' AND subject = :subject"
            ),
            {"subject": apple_sub},
        )
        winner = resolved.first()
        if winner is None:  # pragma: no cover - the insert above guarantees a row
            raise ServiceUnavailableError("failed to link apple identity")
        winner_id = uuid.UUID(str(winner[0]))

        # audit `auth_event`: the identity link is a security-relevant event.
        # The apple_sub / identity token are NOT logged — only the action and the device.
        await self._audit.log(
            EVENT_AUTH_EVENT,
            session=self._session,
            user_id=winner_id,
            payload={"action": AUTH_ACTION_APPLE_IDENTITY_LINKED, "deviceId": device_id},
        )
        return winner_id

    async def _upsert_device(self, device_id: str, user_id: uuid.UUID) -> None:
        """Bind the device to ``user_id``.

        On conflict (the device belonged to another userId — e.g. signing into one's Apple account
        on a shared device) the apple_sub-user WINS and the device is re-pointed to it. No data
        auto-merge (Q-043-2).

        This upsert is also what keeps the PAYMENT circuit correct: if the device stayed bound to
        the old userId, later payments would be credited to the abandoned account.
        """
        await self._session.execute(
            text(
                "INSERT INTO auth_devices (user_id, device_id) VALUES (:user_id, :device_id) "
                "ON CONFLICT (device_id) DO UPDATE "
                "SET user_id = EXCLUDED.user_id, last_seen_at = now()"
            ),
            {"user_id": str(user_id), "device_id": device_id},
        )

    async def refresh(self, refresh_token: str) -> IssuedTokens:
        """Rotate a refresh token into a new pair (single-use). Any problem → 401.

        Unknown / revoked / expired tokens all yield the same 401 without revealing which.
        """
        self._require_issuer()
        token_hash = _hash_refresh(refresh_token)
        result = await self._session.execute(
            text(
                "SELECT id, user_id, device_id, expires_at, used_at, revoked_at "
                "FROM auth_refresh_tokens WHERE token_hash = :token_hash"
            ),
            {"token_hash": token_hash},
        )
        row = result.mappings().first()
        if row is None:
            raise UnauthorizedError("invalid refresh token")

        user_id = uuid.UUID(str(row["user_id"]))
        device_id = str(row["device_id"])

        if row["used_at"] is not None:
            # REUSE. Two explanations — (a) a client retry, (b) the token was STOLEN — and they
            # are indistinguishable. Fail-safe: revoke the entire device chain (BR-AUTH-4).
            await self._revoke_chain(user_id, device_id)
            await self._session.commit()
            raise UnauthorizedError("refresh token reuse detected")

        if row["revoked_at"] is not None:
            raise UnauthorizedError("refresh token revoked")

        expires_at = row["expires_at"]
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise UnauthorizedError("refresh token expired")

        # ⚠ SINGLE-USE IS CLAIMED HERE, ATOMICALLY — the `used_at IS NULL` guard lives in the
        # WHERE of the UPDATE itself, exactly like the balance guard in WalletService.consume().
        # The SELECT above is only a fast pre-check: under READ COMMITTED it takes no lock, so two
        # concurrent /refresh calls with the SAME token would both read used_at=NULL. An
        # unconditional `UPDATE ... WHERE id` would then let BOTH issue a pair — single-use broken
        # and, worse, the reuse-detect silently dead: a thief replaying a stolen token in parallel
        # with the legitimate client would get a valid pair and stay unnoticed.
        #
        # With the conditional UPDATE exactly ONE writer wins (the loser's row-lock wait ends with
        # zero rows matched). RETURNING empty ⇒ somebody else already claimed this token ⇒ it IS a
        # reuse ⇒ revoke the whole device chain and 401.
        claimed = await self._session.scalar(
            text(
                "UPDATE auth_refresh_tokens SET used_at = now() "
                "WHERE id = :id AND used_at IS NULL "
                "RETURNING id"
            ),
            {"id": str(row["id"])},
        )
        if claimed is None:
            await self._revoke_chain(user_id, device_id)
            await self._session.commit()
            raise UnauthorizedError("refresh token reuse detected")

        tokens = await self._issue_pair(user_id, device_id)
        await self._session.commit()
        return tokens

    async def _revoke_chain(self, user_id: uuid.UUID, device_id: str) -> None:
        """Revoke every live refresh token of the device + write the ``auth_event`` audit record."""
        await self._session.execute(
            text(
                "UPDATE auth_refresh_tokens SET revoked_at = now() "
                "WHERE user_id = :user_id AND device_id = :device_id AND revoked_at IS NULL"
            ),
            {"user_id": str(user_id), "device_id": device_id},
        )
        await self._audit.log(
            EVENT_AUTH_EVENT,
            session=self._session,
            user_id=user_id,
            payload={"action": AUTH_ACTION_REFRESH_CHAIN_REVOKED, "deviceId": device_id},
        )
