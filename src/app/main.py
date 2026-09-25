"""FastAPI app factory — registry-driven.

There is **not one domain name in this file**. Routers, OpenAPI tags/description, middleware
rules and lifecycle hooks come from ``DomainRegistry``; the title comes from ``SERVICE_NAME``.
The source had a static list of 18 routers and ``title="claude-ios-backend"`` hardcoded here —
each of which a new service would have had to edit, i.e. fork the core.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api_gateway.middleware import (
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
    SizeLimitMiddleware,
)
from app.api_gateway.rate_limit import close_redis
from app.api_gateway.routers import (
    admin,
    auth,
    billing,
    billing_webhooks,
    generations,
    health,
    policy,
    profile,
    wallet,
)
from app.config import get_settings
from app.db import dispose_engine
from app.errors import AppError
from app.extensions.loader import load_registry
from app.extensions.registry import DomainRegistry
from app.observability.context import get_request_id
from app.observability.logging import configure_logging
from app.observability.metrics import service_info

logger = logging.getLogger("app.main")

# Core routers. `POST /v1/generate` is NOT here — it is a DOMAIN route (the core has no knowledge
# of `params`); it arrives through DomainRegistry.routers, like every domain route.
_CORE_ROUTERS = (
    auth.router,
    policy.router,
    wallet.router,
    generations.router,
    billing.products_router,
    billing.payments_router,
    billing.subscription_router,
    billing.tokens_router,
    billing_webhooks.router,
    admin.router,
    profile.router,
    health.router,
)

_HEALTH_TAG = {
    "name": "Health",
    "description": "Служебные проверки и метрики (без JWT): liveness, readiness, Prometheus.",
}
_CORE_TAGS: tuple[dict[str, str], ...] = (
    {
        "name": "Auth",
        "description": (
            "Получение и обновление токенов. Вход без регистрации по `deviceId` и вход через "
            "Apple (кросс-девайс). Публичные эндпоинты, защищены per-IP лимитом."
        ),
    },
    {
        "name": "Policy",
        "description": (
            "Эффективные права пользователя для UI: можно ли генерировать и почему нет. Та же "
            "функция решения, что и у генерации."
        ),
    },
    {
        "name": "Wallet",
        "description": "Баланс кредитов. Списание и начисление публичных эндпоинтов не имеют.",
    },
    {
        "name": "Products",
        "description": "Каталог продуктов: что можно купить и сколько кредитов это даёт.",
    },
    {
        "name": "Subscription",
        "description": "Статус подписки и синхронизация покупки из App Store.",
    },
    {
        "name": "Tokens",
        "description": "Покупка пакета кредитов (consumable App Store). Требует активной подписки.",
    },
    {
        "name": "Payments",
        "description": "История платежей пользователя по всем каналам.",
    },
    {
        "name": "Billing",
        "description": (
            "Интеграции с платёжными системами: вебхуки (вызывают платёжные сервисы, не клиент) и "
            "создание ссылки на оплату."
        ),
    },
    {
        "name": "Admin",
        "description": (
            "Операторские действия под заголовком `X-Admin-Token`. Пользовательский токен здесь не "
            "авторизует."
        ),
    },
    {
        "name": "Profile",
        "description": "Отображаемое имя и человекочитаемый `accountId`.",
    },
)

_CORE_DESCRIPTION = """\
Backend-ядро сервиса.

### Авторизация
Все `/v1/*` (кроме `/v1/auth/*` и вебхуков) требуют заголовок `Authorization: Bearer
<accessToken>` — JWT (RS256). В claim `sub` лежит `userId`; поле `userId` в теле запроса обязано
совпадать с `sub`, иначе `403`. Endpoint `/health`, `/healthz`, `/ready`, `/metrics` токен не
требуют.

### Блокировки приходят с HTTP 200
Бизнес-блокировка генерации — это успешный ответ `200` с телом `{status: "blocked", blockReason}`,
а не ошибка. Технические ошибки — `4xx`/`5xx` с телом `{error: {code, message, requestId}}`.
"""


def _lifespan(
    registry: DomainRegistry,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Core lifecycle + the domain's own startup/shutdown hooks."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        configure_logging(
            settings.log_level,
            service=settings.service_name,
            version=settings.service_version,
        )
        # The service names itself in METRICS too, not only in logs/OpenAPI: several
        # template-born services scrape into one Prometheus.
        service_info.info(
            {
                "service": settings.service_name,
                "version": settings.service_version,
                "environment": settings.environment,
            }
        )

        # Fail-loud on test-mode verification left enabled outside dev.
        if settings.storekit_test_mode and settings.storekit_test_secret:
            logger.warning(
                "STOREKIT_TEST_MODE is ENABLED — accepting HS256 test transactions. "
                "MUST be false in production."
            )
        if settings.apple_test_mode and settings.apple_test_secret:
            logger.warning(
                "APPLE_TEST_MODE is ENABLED — accepting HS256 Apple identity tokens. "
                "MUST be false in production."
            )

        # A domain may ship its own Prometheus metrics: importing the module registers them in
        # the process-global default registry, which GET /metrics renders.
        if registry.metrics_module:
            importlib.import_module(registry.metrics_module)

        for hook in registry.on_startup:
            await hook()
        yield
        for hook in registry.on_shutdown:
            await hook()

        await dispose_engine()
        await close_redis()

    return lifespan


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "requestId": get_request_id()}},
    )


def create_app() -> FastAPI:
    registry = load_registry()
    settings = get_settings()

    app = FastAPI(
        title=settings.service_title_resolved(),  # env, not a hardcoded product name
        version=settings.service_version,
        description=_CORE_DESCRIPTION + (registry.api_description or ""),
        openapi_tags=[*_CORE_TAGS, *registry.openapi_tags, _HEALTH_TAG],
        lifespan=_lifespan(registry),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # Middleware executes in REVERSE order of registration: CorrelationId runs first (so every
    # later layer, including the 413 response, already has a requestId), then SizeLimit (rejects
    # before the body is parsed), then SecurityHeaders.
    app.add_middleware(
        SecurityHeadersMiddleware,
        exempt_prefixes=registry.security_headers_exempt_prefixes,
    )
    app.add_middleware(
        SizeLimitMiddleware,
        default_limit=settings.size_limit_body,
        rules=registry.body_limit_rules,
    )
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        # The wire `code` is the contract: we serialize exc.code, never exc.message.
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(422, "validation_error", "request validation failed")

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error")
        return _error_response(500, "internal_error", "internal error")

    for router in (*_CORE_ROUTERS, *registry.routers):
        app.include_router(router)

    return app


app = create_app()
