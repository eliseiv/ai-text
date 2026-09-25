"""Admin routes under the isolated ``X-Admin-Token``.

A user JWT cannot authorize anything here — not because a role check rejects it, but because there
is no code on this path that reads a JWT at all. Escalation is impossible by construction, not by a
check somebody could forget or invert.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.admin.service import AdminService
from app.api_gateway.auth import require_admin
from app.api_gateway.rate_limit import enforce_admin_limits
from app.deps import client_ip, get_admin_service
from app.errors import RateLimitedError
from app.schemas.admin import (
    AdminGrantRequest,
    AdminGrantResponse,
    AdminSubscriptionGrantRequest,
    AdminSubscriptionGrantResponse,
    AdminWalletResponse,
)

router = APIRouter(prefix="/v1/admin", tags=["Admin"], dependencies=[Depends(require_admin)])


async def _limit(request: Request) -> None:
    """A dedicated per-IP limit: brute-forcing the admin secret must not be feasible."""
    if not await enforce_admin_limits(ip=client_ip(request)):
        raise RateLimitedError("rate limit exceeded")


@router.post(
    "/wallet/grant",
    response_model=AdminGrantResponse,
    summary="Начислить кредиты",
    description=(
        "Начисляет кредиты существующему пользователю (компенсация, поддержка, промо). "
        "Идемпотентно по обязательному `idempotencyKey`: повтор не начислит дважды; тот же ключ с "
        "другой суммой — `409`. Несуществующий `userId` — `404` (пользователи здесь не "
        "создаются).\n\n"
        "**Одного гранта кредитов часто недостаточно:** без активной подписки доступ всё равно "
        "будет закрыт — кредиты это ресурс, а подписка это право. Для восстановления доступа "
        "нужен ещё `POST /v1/admin/subscription/grant`."
    ),
)
async def admin_wallet_grant(
    request: Request,
    body: AdminGrantRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> AdminGrantResponse:
    await _limit(request)
    result = await service.grant(
        user_id=body.userId,
        credits=body.credits,
        idempotency_key=body.idempotencyKey,
        reason=body.reason,
    )
    return AdminGrantResponse(
        creditsGranted=result.credits_granted,
        newBalance=result.new_balance,
        ledgerTxId=result.ledger_tx_id,
        idempotentReplay=result.idempotent_replay,
    )


@router.post(
    "/subscription/grant",
    response_model=AdminSubscriptionGrantResponse,
    summary="Активировать подписку",
    description=(
        "Активирует подписку без проверки транзакции App Store — для поддержки и компенсаций. "
        "Ровно одно из `expiresAt` / `days`; `expiresAt` обязан быть в будущем. Поле `credits`: "
        "опущено — начислить пакет периода по умолчанию, `0` — активировать без начисления, `N` — "
        "начислить ровно N."
    ),
)
async def admin_subscription_grant(
    request: Request,
    body: AdminSubscriptionGrantRequest,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> AdminSubscriptionGrantResponse:
    await _limit(request)
    result = await service.grant_subscription(
        user_id=body.userId,
        expires_at=body.resolved_expires_at(),
        plan=body.plan,
        credits=body.credits,
        idempotency_key=body.idempotencyKey,
    )
    return AdminSubscriptionGrantResponse(
        status=result.status,
        expiresAt=result.expires_at,
        plan=result.plan,
        creditsGranted=result.credits_granted,
        newBalance=result.new_balance,
        ledgerTxId=result.ledger_tx_id,
        idempotentReplay=result.idempotent_replay,
    )


@router.get(
    "/wallet/{user_id}",
    response_model=AdminWalletResponse,
    summary="Кошелёк пользователя",
    description="Баланс и последние операции пользователя. Несуществующий `userId` — `404`.",
)
async def admin_wallet_view(
    request: Request,
    user_id: uuid.UUID,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> AdminWalletResponse:
    await _limit(request)
    view = await service.get_wallet_view(user_id)
    return AdminWalletResponse(
        userId=view.user_id,
        balance=view.balance,
        updatedAt=view.updated_at,
        recentTransactions=view.recent_transactions,
    )
