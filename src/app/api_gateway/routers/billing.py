"""User-facing billing routes: products, payments history, subscription, token purchase.

Webhooks live in their own routers (different principals, different trust anchors).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, DbSession, get_subscription_service, get_token_purchase_service
from app.errors import RateLimitedError
from app.products import get_products, products_for_channel
from app.schemas.billing import (
    PaymentsResponse,
    PaymentView,
    ProductsResponse,
    ProductView,
    SubscriptionResponse,
    SubscriptionSyncRequest,
    SubscriptionSyncResponse,
    TokenPurchaseRequest,
    TokenPurchaseResponse,
)
from app.subscription.service import SubscriptionService
from app.token_purchase.service import TokenPurchaseService

products_router = APIRouter(prefix="/v1/products", tags=["Products"])
payments_router = APIRouter(prefix="/v1/payments", tags=["Payments"])
subscription_router = APIRouter(prefix="/v1/subscription", tags=["Subscription"])
tokens_router = APIRouter(prefix="/v1/tokens", tags=["Tokens"])


async def _limit(current: CurrentUser) -> None:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")


@products_router.get(
    "",
    response_model=ProductsResponse,
    summary="Каталог продуктов",
    description=(
        "Что можно купить и сколько кредитов это даёт. Отдаётся из того же источника, что и "
        "начисление, поэтому каталог и начисление не могут разойтись. Цена в деньгах здесь не "
        "отдаётся — её знает платёжная система."
    ),
)
async def list_products(
    current: CurrentUser,
    channel: Annotated[
        str | None, Query(description="Показать только продукты этого канала.")
    ] = None,
) -> ProductsResponse:
    await _limit(current)
    items = products_for_channel(channel) if channel else tuple(get_products().values())
    return ProductsResponse(
        products=[
            ProductView(
                productId=p.product_id,
                kind=p.kind,
                credits=p.credits,
                title=p.title,
                channels=sorted(p.channels),
                period=p.period,
            )
            for p in items
        ]
    )


@payments_router.get(
    "",
    response_model=PaymentsResponse,
    summary="История платежей",
    description=(
        "Платежи пользователя по всем каналам. Возвращаются только свои: параметра «чьи платежи» "
        "не существует. Внутренние данные платежа (`payload`) наружу не отдаются."
    ),
)
async def list_payments(
    current: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PaymentsResponse:
    await _limit(current)
    rows = (
        await session.execute(
            text(
                "SELECT id, channel, kind, product_id, status, credits_granted, amount, currency, "
                "received_at FROM payments WHERE user_id = :uid "
                "ORDER BY received_at DESC LIMIT :n"
            ),
            {"uid": str(current.user_id), "n": limit},
        )
    ).all()
    return PaymentsResponse(
        items=[
            PaymentView(
                id=r[0],
                channel=r[1],
                kind=r[2],
                productId=r[3],
                status=r[4],
                creditsGranted=int(r[5]),
                amount=r[6],
                currency=r[7],
                receivedAt=r[8],
            )
            for r in rows
        ],
        nextCursor=None,
    )


@subscription_router.post(
    "/sync",
    response_model=SubscriptionSyncResponse,
    summary="Синхронизация подписки",
    description=(
        "Проверяет транзакцию App Store и активирует подписку, начисляя кредиты периода. "
        "Повторная отправка той же транзакции (restore purchases) не начисляет повторно. "
        "`422` — транзакция не прошла проверку, проверка недоступна, продукт неизвестен или не "
        "продаётся в этом канале."
    ),
)
async def subscription_sync(
    current: CurrentUser,
    body: SubscriptionSyncRequest,
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionSyncResponse:
    await _limit(current)
    result = await service.sync(current.user_id, body.transaction)
    return SubscriptionSyncResponse(
        status=result.status,
        plan=result.plan,
        expiresAt=result.expires_at,
        creditsGranted=result.credits_granted,
        newBalance=result.new_balance,
        idempotentReplay=result.idempotent_replay,
    )


@subscription_router.get(
    "",
    response_model=SubscriptionResponse,
    summary="Статус подписки",
    description=(
        "Текущий статус подписки — уже с учётом истечения срока, то есть ровно то значение, "
        "которое увидит проверка доступа."
    ),
)
async def get_subscription(
    current: CurrentUser,
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionResponse:
    await _limit(current)
    view = await service.get_subscription(current.user_id)
    return SubscriptionResponse(status=view.status, plan=view.plan, expiresAt=view.expires_at)


@tokens_router.post(
    "/purchase",
    response_model=TokenPurchaseResponse,
    summary="Покупка пакета токенов",
    description=(
        "Начисляет кредиты за consumable-покупку в App Store. Тело не содержит ни числа кредитов, "
        "ни `productId` — они берутся из проверенной транзакции и серверного каталога. Повтор той "
        "же транзакции не начисляет повторно.\n\n"
        "**Требуется активная подписка** (`403 subscription_required`). Проверяйте подписку "
        "(`GET /v1/subscription`) **до** инициации покупки в StoreKit: иначе пользователь заплатит "
        "Apple, а начисления не будет — потребуется ручной возврат."
    ),
)
async def purchase_tokens(
    current: CurrentUser,
    body: TokenPurchaseRequest,
    service: Annotated[TokenPurchaseService, Depends(get_token_purchase_service)],
) -> TokenPurchaseResponse:
    await _limit(current)
    result = await service.purchase(current.user_id, body.transaction)
    return TokenPurchaseResponse(
        creditsAdded=result.credits_added,
        newBalance=result.new_balance,
        productId=result.product_id,
        transactionId=result.transaction_id,
        idempotentReplay=result.idempotent_replay,
    )


__all__ = [
    "payments_router",
    "products_router",
    "subscription_router",
    "tokens_router",
]
