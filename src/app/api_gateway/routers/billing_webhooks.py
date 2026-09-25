"""Payment webhooks + the RU checkout. Three different principals, three different trust anchors.

| endpoint | who calls it | trust anchor |
|---|---|---|
| ``/v1/billing/adapty/webhook`` | Adapty (M2M) | a static bearer secret, constant-time |
| ``/v1/billing/cloudpayments/webhook`` | anyone — it is PUBLIC | **our own verify call** |
| ``/v1/billing/cloudpayments/checkout`` | our client | the user's JWT |

The webhook bodies are read RAW: a Pydantic model would answer ``422`` to the platform's
verification ping (making the webhook impossible to configure) and would turn a payload-format
drift into an infinite retry storm.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.rate_limit import (
    enforce_cloudpayments_webhook_limits,
    enforce_other_limits,
)
from app.billing_adapty.auth import require_adapty_webhook
from app.billing_adapty.service import AdaptyWebhookService
from app.billing_cloudpayments.auth import require_cloudpayments_webhook
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.deps import (
    CurrentUser,
    client_ip,
    get_adapty_webhook_service,
    get_cloudpayments_checkout_client,
    get_cloudpayments_webhook_service,
)
from app.errors import RateLimitedError
from app.schemas.billing import CheckoutRequest, CheckoutResponse, CloudPaymentsAck, WebhookAck

router = APIRouter(prefix="/v1/billing", tags=["Billing"])


@router.post(
    "/adapty/webhook",
    response_model=WebhookAck,
    dependencies=[Depends(require_adapty_webhook)],
    summary="Вебхук подписок Adapty",
    description=(
        "Приём событий жизненного цикла подписки: покупка, продление, истечение, отмена. "
        "Вызывает Adapty, не клиент; авторизация — статический секрет вебхука.\n\n"
        "После успешной авторизации сервис отвечает `200` на **любое** тело: нераспознанное "
        "событие возвращается как `ignored` с машинной причиной. Это не «проглатывание ошибок» — "
        "повтор нераспознанного тела ничего не исправит, а `5xx` вызвал бы бесконечные ретраи. "
        "`5xx` возвращается только на реальный сбой (например, недоступна БД), где повтор как раз "
        "уместен."
    ),
)
async def adapty_webhook(
    request: Request,
    service: Annotated[AdaptyWebhookService, Depends(get_adapty_webhook_service)],
) -> WebhookAck:
    outcome = await service.handle(await request.body())
    return WebhookAck(result=outcome.result, reason=outcome.reason)


@router.post(
    "/cloudpayments/webhook",
    response_model=CloudPaymentsAck,
    dependencies=[Depends(require_cloudpayments_webhook)],
    summary="Вебхук платёжного сервиса (RU)",
    description=(
        "Публичный колбэк платёжного сервиса. Он служит **триггером**: начисление выполняется "
        "только после того, как сервер сам подтвердит платёж запросом к платёжному сервису своим "
        "ключом. Поэтому поддельный колбэк безвреден — подтверждения не будет, и кредиты не "
        "начислятся.\n\n"
        "Ошибка подтверждения возвращает `500`: платёжный сервис пришлёт колбэк повторно, и платёж "
        "не потеряется."
    ),
)
async def cloudpayments_webhook(
    request: Request,
    service: Annotated[CloudPaymentsWebhookService, Depends(get_cloudpayments_webhook_service)],
) -> CloudPaymentsAck:
    # The endpoint is PUBLIC: the per-IP limit is mandatory, not decorative. Without it, forged
    # callbacks would amplify into a storm of OUR outgoing verification calls.
    if not await enforce_cloudpayments_webhook_limits(ip=client_ip(request)):
        raise RateLimitedError("rate limit exceeded")
    await service.handle(await request.body())
    return CloudPaymentsAck(code=0)


@router.post(
    "/cloudpayments/checkout",
    response_model=CheckoutResponse,
    summary="Создать ссылку на оплату (RU)",
    description=(
        "Создаёт ссылку на оплату для выбранного продукта. Идентификатор пользователя берётся из "
        "токена и уходит платёжному сервису с сервера — поэтому колбэк об оплате гарантированно "
        "находит пользователя. Тело не содержит ни `userId`, ни суммы.\n\n"
        "`422` — неизвестный продукт или продукт не продаётся в этом канале; `502` — платёжный "
        "сервис недоступен; `503` — канал не настроен на этом инстансе."
    ),
)
async def cloudpayments_checkout(
    current: CurrentUser,
    body: CheckoutRequest,
    client: Annotated[CloudPaymentsCheckoutClient, Depends(get_cloudpayments_checkout_client)],
) -> CheckoutResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    client.require_configured()
    client.validate(body.productId)
    result = await client.create_payment_link(
        user_id=current.user_id,  # ⚠ from the JWT sub, never from the body
        product_id=body.productId,
        customer_email=str(body.customerEmail),
    )
    return CheckoutResponse(
        paymentId=result.payment_id,
        paymentUrl=result.payment_url,
        status=result.status,
        expiresAt=result.expires_at,
    )
