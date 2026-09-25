"""Schemas of the billing surface (products, payments, subscription, tokens, checkout).

Note what the purchase bodies do NOT contain: no ``credits``, no ``productId``, no ``userId``,
no ``amount``. The client cannot influence the credited amount with ANY field — the productId comes
from the cryptographically verified payload, the amount from the server-side ``PRODUCTS`` map, and
the addressee from the JWT ``sub``. ``extra='forbid'`` turns an attempt into a ``422``.
"""

from __future__ import annotations

import datetime
import decimal
import uuid

from pydantic import EmailStr, Field

from app.schemas.common import StrictModel

# A StoreKit signed transaction (compact JWS). Never logged (redaction denylist: `transaction`).
_TRANSACTION_MAX_CHARS = 16384


class ProductView(StrictModel):
    productId: str = Field(description="Идентификатор продукта в платёжной системе.")
    kind: str = Field(description="`subscription` — подписка, `tokens` — пакет кредитов.")
    credits: int = Field(description="Сколько кредитов даёт покупка. Это наш контракт с клиентом.")
    title: str = Field(description="Отображаемое название.")
    channels: list[str] = Field(description="Каналы, в которых продукт можно купить.")
    period: str | None = Field(
        default=None, description="Период подписки (ISO-8601: `P1W`, `P1M`, `P1Y`). Пакеты — нет."
    )


class ProductsResponse(StrictModel):
    products: list[ProductView] = Field(
        description=(
            "Каталог. Отдаётся из того же источника, что и начисление, — расхождение «в UI один "
            "пакет, начислили другой» невозможно. Цена в деньгах здесь не отдаётся: её знает "
            "платёжная система."
        )
    )


class PaymentView(StrictModel):
    id: uuid.UUID
    channel: str = Field(description="`apple_storekit` | `adapty` | `cloudpayments`.")
    kind: str
    productId: str | None = None
    status: str = Field(
        description=(
            "`granted` — начислено; `replayed` — событие валидно, но грант уже был (0 кредитов); "
            "`no_grant` — событие не начисляет по смыслу (истечение/отмена); `rejected` — "
            "неизвестный продукт или продукт чужого канала (0 кредитов)."
        )
    )
    creditsGranted: int
    amount: decimal.Decimal | None = Field(
        default=None, description="Информационно (сверка с выпиской). Кредиты от неё не зависят."
    )
    currency: str | None = None
    receivedAt: datetime.datetime


class PaymentsResponse(StrictModel):
    items: list[PaymentView]
    nextCursor: str | None = None


class SubscriptionSyncRequest(StrictModel):
    transaction: str = Field(
        min_length=1,
        max_length=_TRANSACTION_MAX_CHARS,
        description="Подписанная транзакция App Store. Не логируется.",
    )


class SubscriptionSyncResponse(StrictModel):
    status: str
    plan: str | None = None
    expiresAt: datetime.datetime | None = None
    creditsGranted: int
    newBalance: int
    idempotentReplay: bool = Field(
        description="`true` — транзакция уже обработана (restore purchases); кредиты не начислены "
        "повторно."
    )


class SubscriptionResponse(StrictModel):
    status: str = Field(
        description="`active` | `expired` | `none` — **после** ленивого истечения, то есть ровно "
        "то, что увидит проверка доступа."
    )
    plan: str | None = None
    expiresAt: datetime.datetime | None = None


class TokenPurchaseRequest(StrictModel):
    """Тело НЕ содержит ни `credits`, ни `productId` — иначе клиент влиял бы на сумму начисления."""

    transaction: str = Field(
        min_length=1,
        max_length=_TRANSACTION_MAX_CHARS,
        description="Подписанная consumable-транзакция App Store. Не логируется.",
    )


class TokenPurchaseResponse(StrictModel):
    creditsAdded: int
    newBalance: int
    productId: str
    transactionId: str
    idempotentReplay: bool


class CheckoutRequest(StrictModel):
    """Тело НЕ содержит `userId`/`appId`/`amount`: адресат берётся из JWT `sub`."""

    productId: str = Field(min_length=1, max_length=128)
    customerEmail: EmailStr = Field(
        description="Передаётся платёжному сервису. Не логируется и не сохраняется."
    )


class CheckoutResponse(StrictModel):
    paymentId: str
    paymentUrl: str
    status: str
    expiresAt: str | None = None


class WebhookAck(StrictModel):
    """Ответ вебхука Adapty."""

    result: str
    reason: str | None = None


class CloudPaymentsAck(StrictModel):
    """Ответ вебхука в формате CloudPayments: `{"code": 0}` = принято."""

    code: int = 0
