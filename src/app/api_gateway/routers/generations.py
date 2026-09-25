"""Reading generations: history, one generation, aggregates.

``POST /v1/generate`` is NOT here — it is a DOMAIN route (the core has no knowledge of ``params``).
These reads are core, free of charge, and owner-scoped: a foreign generation is a ``404``, never a
``403`` — we do not reveal that someone else's generation exists.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_generations_repository
from app.errors import GenerationNotFoundError, RateLimitedError
from app.generation.repository import GenerationRow, GenerationsRepository
from app.schemas.generation import (
    GenerationDetail,
    GenerationsListResponse,
    GenerationStatsItem,
    GenerationStatsResponse,
    GenerationView,
)

router = APIRouter(prefix="/v1/generations", tags=["Generation"])

# ⚠ The filter is typed as the ENUM, not as `str`. The value is CAST to `generation_status` in SQL,
# so anything outside the enum (`?status=bogus`, and even `?status=Succeeded`) makes PostgreSQL
# raise 22P02 and the endpoint answers **500** — a user-controlled input turning into a server
# error. Typed here, FastAPI rejects it with 422 before any query is built. (`kind` is free TEXT
# with no cast, so it carries no such hazard.)
GenerationStatusFilter = Literal["pending", "running", "succeeded", "failed", "canceled"]


def _view(row: GenerationRow) -> GenerationView:
    return GenerationView(
        id=row.id,
        kind=row.kind,
        model=row.model,
        status=row.status,
        creditsCharged=row.credits_charged,
        units=row.units,
        unitKind=row.unit_kind,
        totalTokens=row.total_tokens,
        latencyMs=row.latency_ms,
        errorCode=row.error_code,
        providerRef=row.provider_ref,
        createdAt=row.created_at,
        completedAt=row.completed_at,
    )


@router.get(
    "",
    response_model=GenerationsListResponse,
    summary="История генераций",
    description=(
        "Свои генерации, свежие сверху. Фильтры `kind` и `status`. Результат генерации (`output`) "
        "в списке не отдаётся — только в карточке одной генерации."
    ),
)
async def list_generations(
    current: CurrentUser,
    repo: Annotated[GenerationsRepository, Depends(get_generations_repository)],
    kind: Annotated[str | None, Query(max_length=64)] = None,
    status: Annotated[GenerationStatusFilter | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> GenerationsListResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    rows = await repo.list_for_user(current.user_id, kind=kind, status=status, limit=limit)
    return GenerationsListResponse(items=[_view(r) for r in rows], nextCursor=None)


@router.get(
    "/stats",
    response_model=GenerationStatsResponse,
    summary="Статистика генераций",
    description="Агрегаты по видам генераций: сколько запусков, успехов, отказов, кредитов.",
)
async def generation_stats(
    current: CurrentUser,
    repo: Annotated[GenerationsRepository, Depends(get_generations_repository)],
) -> GenerationStatsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    totals = await repo.stats(current.user_id)
    return GenerationStatsResponse(totals=[GenerationStatsItem(**t) for t in totals])


@router.get(
    "/{generation_id}",
    response_model=GenerationDetail,
    summary="Одна генерация",
    description=(
        "Карточка генерации вместе с результатом. Это же — точка получения результата для "
        "генераций, выполняющихся в фоне: пока `status` = `pending`/`running`, запросите позже. "
        "Чужая или несуществующая генерация — `404`."
    ),
)
async def get_generation(
    current: CurrentUser,
    generation_id: uuid.UUID,
    repo: Annotated[GenerationsRepository, Depends(get_generations_repository)],
) -> GenerationDetail:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    row = await repo.get(current.user_id, generation_id)
    if row is None:
        raise GenerationNotFoundError("generation not found")
    base = _view(row)
    return GenerationDetail(**base.model_dump(), output=row.meta.get("output"))
