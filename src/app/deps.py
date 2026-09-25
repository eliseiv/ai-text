"""FastAPI dependencies of the CORE: db session, auth, owner check, client ip.

CORE wiring only. No domain service is imported here — a domain wires its own dependencies inside
``app/domain/``. Core service factories (wallet / policy / subscription / billing /
generation / admin / profile) are added by their own phases as those modules land.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.service import AdminService
from app.api_gateway.auth import AuthenticatedUser, get_jwt_verifier
from app.api_gateway.openapi_security import bearer_scheme
from app.audit.service import AuditService
from app.auth.apple import get_apple_verifier
from app.auth.issuer import TokenIssuer
from app.auth.service import AuthService
from app.billing.payments import PaymentsJournal
from app.billing_adapty.service import AdaptyWebhookService
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.billing_cloudpayments.verify import CloudPaymentsVerifyClient
from app.config import get_settings
from app.db import session_scope
from app.errors import ForbiddenError, UnauthorizedError
from app.generation.registry import get_pricing, get_provider
from app.generation.repository import GenerationsRepository
from app.generation.service import GenerationService
from app.observability.context import set_user_id
from app.profile.service import ProfileService
from app.subscription.service import SubscriptionService
from app.subscription.storekit import get_storekit_verifier
from app.token_purchase.service import TokenPurchaseService
from app.wallet.service import WalletService


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in session_scope():
        yield session


def verify_bearer_token(authorization: str | None) -> AuthenticatedUser:
    """Verify the Bearer JWT (signature/exp/iss/aud) and extract the trusted subject.

    Pure and side-effect-free (no DB, no token logging). Identity comes exclusively from the
    VERIFIED ``sub`` claim — never from a request body.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    return get_jwt_verifier().verify(token)


async def provision_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Lazy, idempotent provisioning of the ``users`` row for a VERIFIED subject.

    Runs in the same per-request session as the downstream FK-bearing inserts, so the row is
    visible to every later statement of this transaction and is committed together with them.
    ``ON CONFLICT (id) DO NOTHING`` is atomic in PostgreSQL: concurrent first requests for one
    ``sub`` cannot race or duplicate, and an existing user's ``trial_used``/``created_at`` are
    never overwritten.

    Webhooks are the deliberate exception (invariant 10): they have no trusted
    JWT ``sub``, so they never provision — an unknown user yields ``200 ignored/user_not_found``.
    """
    await session.execute(
        text("INSERT INTO users (id) VALUES (:sub) ON CONFLICT (id) DO NOTHING"),
        {"sub": str(user_id)},
    )


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthenticatedUser:
    """Authenticate the request, then lazily provision the user.

    ORDER IS CRITICAL: verify FIRST, provision SECOND. An invalid/forged token raises 401 before
    any row is created — there is no "flood ``users`` with junk by sending random tokens" vector.
    """
    # Re-assemble the canonical "Bearer <token>" string so verify_bearer_token keeps its
    # header-shaped signature and its 401 semantics.
    authorization = f"Bearer {credentials.credentials}" if credentials is not None else None
    user = verify_bearer_token(authorization)
    set_user_id(str(user.user_id))
    # FastAPI caches `get_db` per request, so this is the exact session the service dependencies
    # receive — the upsert lands in the same transaction as their FK-bearing inserts.
    await provision_user(session, user.user_id)
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def require_owner(body_user_id: uuid.UUID, current: AuthenticatedUser) -> None:
    """``userId`` in the body MUST equal the verified ``sub`` (403 otherwise)."""
    if body_user_id != current.user_id:
        raise ForbiddenError("userId does not match authenticated subject")


def get_audit() -> AuditService:
    """``AuditService`` is stateless: the caller passes ITS session to ``log()``, so the audit row
    lands in the same transaction as the action."""
    return AuditService()


_token_issuer_singleton: TokenIssuer | None = None


def get_token_issuer() -> TokenIssuer:
    """Process-wide RS256 issuer (reads the key pair once from the cached settings)."""
    global _token_issuer_singleton
    if _token_issuer_singleton is None:
        _token_issuer_singleton = TokenIssuer(get_settings())
    return _token_issuer_singleton


def get_auth_service(session: DbSession) -> AuthService:
    return AuthService(
        session,
        get_token_issuer(),
        get_settings(),
        get_apple_verifier(),
        AuditService(),
    )


def get_wallet_service(session: DbSession) -> WalletService:
    """The wallet shares the REQUEST session, so its ledger write, balance update and audit record
    commit in the same transaction as the caller's action."""
    return WalletService(session, AuditService())


def _journal(session: AsyncSession) -> PaymentsJournal:
    """The ONE journal every channel goes through. Shares the caller's session — layer 1 and
    layer 2 of ``record_and_grant()`` must commit together."""
    return PaymentsJournal(WalletService(session, AuditService()), AuditService())


def get_subscription_service(session: DbSession) -> SubscriptionService:
    return SubscriptionService(session, get_storekit_verifier(), _journal(session))


def get_token_purchase_service(session: DbSession) -> TokenPurchaseService:
    return TokenPurchaseService(session, get_storekit_verifier(), _journal(session))


def get_generations_repository(session: DbSession) -> GenerationsRepository:
    return GenerationsRepository(session)


def get_generation_service(session: DbSession) -> GenerationService:
    """THE single entry point of a generation. A DOMAIN router calls ``run()`` — and gets policy,
    idempotency, pricing, the debit, accounting, metrics and audit for free."""
    return GenerationService(
        session=session,
        repo=GenerationsRepository(session),
        wallet=WalletService(session, AuditService()),
        audit=AuditService(),
        provider=get_provider(),
        pricing=get_pricing(),
        settings=get_settings(),
    )


def get_adapty_webhook_service(session: DbSession) -> AdaptyWebhookService:
    return AdaptyWebhookService(session, _journal(session))


def get_cloudpayments_verify_client() -> CloudPaymentsVerifyClient:
    return CloudPaymentsVerifyClient(get_settings())


def get_cloudpayments_webhook_service(session: DbSession) -> CloudPaymentsWebhookService:
    return CloudPaymentsWebhookService(
        session, _journal(session), get_settings(), get_cloudpayments_verify_client()
    )


def get_cloudpayments_checkout_client() -> CloudPaymentsCheckoutClient:
    return CloudPaymentsCheckoutClient(get_settings())


def get_admin_service(session: DbSession) -> AdminService:
    return AdminService(session, WalletService(session, AuditService()), AuditService())


def get_profile_service(session: DbSession) -> ProfileService:
    return ProfileService(session)


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in get_settings().trusted_proxy_networks())


def client_ip(request: Request) -> str | None:
    """Resolve the real client IP, respecting a trusted reverse-proxy chain (anti-spoofing).

    The API runs behind Traefik, so the socket peer is the proxy, not the client. We honour
    ``X-Forwarded-For`` / ``X-Real-IP`` ONLY when the immediate peer is a configured trusted proxy
    — otherwise those headers are attacker-controlled. From a trusted chain we take the
    ``(hop_count + 1)``-th entry FROM THE RIGHT (the last address inserted by infrastructure we do
    not control), never the spoofable left-most one.

    ``TRUSTED_PROXY_IPS=""`` (default) => headers ignored, socket peer used — fail-safe. In prod
    it MUST list the ``web`` network subnet, otherwise every client looks like Traefik and per-IP
    rate limiting silently stops working.
    """
    peer = request.client.host if request.client is not None else None
    if peer is None or not _is_trusted_proxy(peer):
        return peer

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
        if hops:
            hop_count = max(get_settings().trusted_proxy_hop_count, 1)
            index = len(hops) - hop_count - 1
            if index < 0:
                index = 0
            return hops[index]

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return peer
