"""OpenAPI security schemes.

Declares the schemes so Swagger shows the lock icon and Authorize works:

* ``bearerAuth`` (HTTP Bearer JWT) — user ``/v1/*`` endpoints **and all domain endpoints**;
* ``adminToken`` (apiKey header ``X-Admin-Token``) — ``/v1/admin/*``;
* ``adaptyWebhook`` (static bearer) — the Adapty webhook (called by Adapty, not by a client);
* ``cloudPaymentsWebhook`` — decorative only: that webhook is PUBLIC.

All are ``SecurityBase`` instances consumed as dependencies inside ``deps.get_current_user`` /
``auth.require_admin``, so they contribute the scheme to each operation's OpenAPI ``security``
WITHOUT adding a duplicate header *parameter*. ``auto_error=False`` keeps them from raising
before our own 401/constant-time checks — the real verification stays in the dependencies.
"""

from __future__ import annotations

from fastapi.security import APIKeyHeader, HTTPBearer

bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    bearerFormat="JWT",
    auto_error=False,
    description=(
        "JWT (RS256). В claim `sub` — userId; `userId` в теле запроса обязан совпадать с `sub`, "
        "иначе `403`. Введите токен как `Bearer <JWT>` через кнопку Authorize — он применится ко "
        "всем защищённым вызовам. Реальная проверка подписи/exp/iss/aud выполняется на сервере."
    ),
)

admin_scheme = APIKeyHeader(
    name="X-Admin-Token",
    scheme_name="adminToken",
    auto_error=False,
    description=(
        "Изолированный admin-токен. Вставьте секрет в заголовок `X-Admin-Token` через Authorize. "
        "Пользовательский JWT admin-действия не авторизует. Реальная constant-time проверка — "
        "на сервере."
    ),
)

adapty_webhook_scheme = HTTPBearer(
    scheme_name="adaptyWebhook",
    auto_error=False,
    description=(
        "Статический bearer-секрет вебхука Adapty (`ADAPTY_WEBHOOK_SECRET`). Вызывает Adapty, не "
        "клиент. НЕ пользовательский JWT и НЕ admin-токен. Реальная constant-time проверка — "
        "на сервере."
    ),
)

cloudpayments_webhook_scheme = HTTPBearer(
    scheme_name="cloudPaymentsWebhook",
    auto_error=False,
    description=(
        "Публичный вебхук платёжного агрегатора (вызывает агрегатор, не клиент). Заголовок "
        "`Authorization` не требуется и не блокирует приём — он лишь наблюдается в логах. "
        "Начисление выполняется только после подтверждения платежа через платёжный сервис."
    ),
)
