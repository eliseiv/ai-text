"""``POST /v1/generate`` — the DOMAIN route (the sample the template ships).

This file is the whole answer to "what does a new service actually write?". It is ~15 lines: take
the request, call ``GenerationService.run()``, return the outcome. Policy, the idempotency anchor,
the provider call, pricing, the debit, the accounting row, the metrics and the audit are all done
by the core, and the domain cannot get them wrong because it never sees them.

The route lives HERE and not in the core on purpose: the core has no knowledge of ``params``. A
real service replaces this body with its own request schema and its own provider — and touches no
core file (that is the AC-9 acceptance criterion).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.api_gateway.rate_limit import enforce_generation_limits
from app.deps import CurrentUser, client_ip, get_generation_service
from app.errors import RateLimitedError
from app.generation.registry import get_provider
from app.generation.service import GenerationService
from app.schemas.generation import GenerateRequest, GenerateResponse

router = APIRouter(prefix="/v1", tags=["Generation"])


@router.post(
    "/generate",
    response_model=GenerateResponse,
    summary="Запустить генерацию",
    description=(
        "Запускает генерацию и возвращает результат.\n\n"
        "Заголовок `Idempotency-Key` (необязательный): повторный запрос с тем же значением не "
        "запускает генерацию заново и не списывает кредиты повторно.\n\n"
        "Если генерация недоступна (нет подписки, кончились кредиты, израсходована бесплатная "
        'попытка) — ответ `200` со `status: "blocked"` и полем `blockReason`. Это не ошибка: '
        "запрос корректен, а генерация просто не запускалась и ничего не списано.\n\n"
        "`409 already_in_progress` — генерация с тем же `Idempotency-Key` ещё выполняется, "
        "результат будет: заберите его по `GET /v1/generations/{id}`. `409 too_many_inflight` — "
        "слишком много одновременных генераций: дождитесь завершения любой и повторите."
    ),
)
async def generate(
    request: Request,
    current: CurrentUser,
    body: GenerateRequest,
    service: Annotated[GenerationService, Depends(get_generation_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> GenerateResponse:
    if not await enforce_generation_limits(user_id=current.user_id, ip=client_ip(request)):
        raise RateLimitedError("rate limit exceeded")

    outcome = await service.run(
        user_id=current.user_id,  # from the JWT sub — never from the body
        kind=get_provider().kind,
        params=body.params,
        model=body.model,
        idempotency_key=idempotency_key,
        request_id=getattr(request.state, "request_id", ""),
    )
    return GenerateResponse(
        status=outcome.status,
        generationId=outcome.generation_id,
        blockReason=outcome.block_reason,
        output=outcome.output,
        usage=outcome.usage,
        creditsCharged=outcome.credits_charged,
        newBalance=outcome.new_balance,
        idempotentReplay=outcome.idempotent_replay,
    )
