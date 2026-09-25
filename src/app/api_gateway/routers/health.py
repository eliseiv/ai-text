"""Service routes: /health, /healthz, /ready, /metrics."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Header, Response
from sqlalchemy import text

from app.api_gateway.rate_limit import redis_ping
from app.config import get_settings
from app.db import get_sessionmaker
from app.generation.inflight import refresh_generations_inflight
from app.observability.metrics import render_metrics

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    summary="Liveness-проверка",
    description='Простая проверка, что процесс жив. JWT не требуется. Всегда `200 {status: "ok"}`.',
)
@router.get(
    "/healthz",
    summary="Liveness-проверка (алиас /health)",
    description=(
        "Алиас `GET /health` для healthcheck и smoke. Публичный, без JWT. "
        'Всегда `200 {status: "ok"}`.'
    ),
)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    summary="Readiness-проверка",
    description=(
        "Проверяет готовность зависимостей (PostgreSQL, Redis). JWT не требуется. `200`, если "
        "обе доступны, иначе `503`. Тело: статус каждой зависимости."
    ),
)
async def ready(response: Response) -> dict[str, str]:
    """Readiness = the deploy's source of truth (compose healthcheck + the deploy gate)."""
    db_ok = False
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001 - a readiness probe reports, it never raises
        db_ok = False
    redis_ok = await redis_ping()
    if not (db_ok and redis_ok):
        response.status_code = 503
    return {"db": "ok" if db_ok else "down", "redis": "ok" if redis_ok else "down"}


@router.get(
    "/metrics",
    summary="Метрики Prometheus",
    description=(
        "Prometheus exposition для скрейпинга. JWT не требуется; защищён сетью и/или scrape-"
        "токеном (`X-Scrape-Token`). При неверном токене — `403`."
    ),
)
async def metrics(
    x_scrape_token: Annotated[str | None, Header()] = None,
) -> Response:
    settings = get_settings()
    expected = settings.metrics_scrape_token
    # Constant-time compare (as for the admin token): a plain `!=` on a secret leaks its prefix
    # through response timing. An empty token means "protected by network policy" — no check.
    if expected and not (
        x_scrape_token is not None and hmac.compare_digest(x_scrape_token, expected)
    ):
        return Response(status_code=403)

    # ⚠ GAUGES MUST BE FILLED BEFORE THE EXPOSITION IS RENDERED.
    # A gauge nobody ever `.set()`s never appears in the exposition at all — and every alert over
    # it is DEAD (a PromQL expression over a non-existent series matches nothing, forever). That is
    # exactly the class of defect the whole observability rule-set exists to remove, and it hides
    # perfectly: the alert-rule tests pass, because they feed SYNTHETIC series and never check that
    # anybody publishes the real ones.
    await refresh_generations_inflight()

    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
