"""Chat and summaries of a topic."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.domain.routers.deps import Chat, GenerationUserId, IdempotencyKey, Summaries, UserId
from app.domain.schemas import (
    AskRequest,
    MessagesPage,
    MessageView,
    SummaryPriceView,
    SummaryRequest,
    SummaryView,
)

router = APIRouter(prefix="/v1", tags=["Chat"])
summaries_router = APIRouter(prefix="/v1", tags=["Summaries"])

_ASYNC_NOTE = (
    "Ответ готовится в фоне: получите `status: queued`, затем опрашивайте объект по `id` до "
    "`succeeded` / `failed` / `blocked` / `canceled`. Списание — только за готовый результат."
)


@router.post(
    "/topics/{topic_id}/messages",
    response_model=MessageView,
    status_code=201,
    summary="Задать вопрос",
    description=(
        _ASYNC_NOTE
        + "\n\nИспользуются только готовые выбранные материалы темы (или подмножество из "
        "`sourceIds`). Нет выбранных — `422 no_sources_selected`; не готовы — "
        "`422 no_ready_sources`. Неготовые материалы перечислены в `skippedSources`.\n\n"
        "Нет доступа — `status: blocked` + `blockReason` (вопрос сохранён, ничего не списано); "
        "после покупки вызовите `retry`."
    ),
)
async def ask(
    user_id: GenerationUserId,
    topic_id: uuid.UUID,
    body: AskRequest,
    chat: Chat,
    key: IdempotencyKey,
) -> MessageView:
    return await chat.ask(
        user_id, topic_id, question=body.question, source_ids=body.sourceIds, idempotency_key=key
    )


@router.get("/topics/{topic_id}/messages", response_model=MessagesPage, summary="История чата")
async def history(
    user_id: UserId,
    topic_id: uuid.UUID,
    chat: Chat,
    cursor: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MessagesPage:
    return await chat.list(user_id, topic_id, cursor=cursor, limit=limit)


@router.get(
    "/topics/{topic_id}/messages/{message_id}", response_model=MessageView, summary="Сообщение"
)
async def get_message(
    user_id: UserId, topic_id: uuid.UUID, message_id: uuid.UUID, chat: Chat
) -> MessageView:
    return await chat.get(user_id, topic_id, message_id)


@router.post(
    "/topics/{topic_id}/messages/{message_id}/cancel",
    response_model=MessageView,
    summary="Отменить ожидание ответа",
    description="Отменённый запрос не списывается.",
)
async def cancel_message(
    user_id: UserId, topic_id: uuid.UUID, message_id: uuid.UUID, chat: Chat
) -> MessageView:
    return await chat.cancel(user_id, topic_id, message_id)


@router.post(
    "/topics/{topic_id}/messages/{message_id}/retry",
    response_model=MessageView,
    summary="Повторить вопрос",
    description=(
        "Для `failed` / `blocked` / `canceled`. Источники выбираются заново: удалённые и "
        "исключённые материалы не используются."
    ),
)
async def retry_message(
    user_id: GenerationUserId, topic_id: uuid.UUID, message_id: uuid.UUID, chat: Chat
) -> MessageView:
    return await chat.retry(user_id, topic_id, message_id)


@summaries_router.post(
    "/topics/{topic_id}/summaries",
    response_model=SummaryView,
    status_code=201,
    summary="Построить конспект",
    description=(
        _ASYNC_NOTE
        + "\n\nКонспект охватывает весь текст выбранных материалов; если прочитана только часть, "
        "`coverage.partial = true`. Конспект по тем же версиям уже есть — он возвращается "
        "(`reused`), без списания."
    ),
)
async def create_summary(
    user_id: GenerationUserId,
    topic_id: uuid.UUID,
    body: SummaryRequest,
    summaries: Summaries,
    key: IdempotencyKey,
) -> SummaryView:
    return await summaries.create(user_id, topic_id, source_ids=body.sourceIds, idempotency_key=key)


@summaries_router.get(
    "/topics/{topic_id}/summaries/price",
    response_model=SummaryPriceView,
    summary="Стоимость конспекта",
    description="Точная цена конспекта по выбранным готовым материалам — до запуска.",
)
async def summary_price_view(
    user_id: UserId,
    topic_id: uuid.UUID,
    summaries: Summaries,
    sourceIds: Annotated[list[uuid.UUID] | None, Query()] = None,  # noqa: N803
) -> SummaryPriceView:
    return await summaries.price(user_id, topic_id, sourceIds)


@summaries_router.get(
    "/topics/{topic_id}/summaries/latest", response_model=SummaryView, summary="Последний конспект"
)
async def latest_summary(user_id: UserId, topic_id: uuid.UUID, summaries: Summaries) -> SummaryView:
    return await summaries.latest(user_id, topic_id)


@summaries_router.get("/summaries/{summary_id}", response_model=SummaryView, summary="Конспект")
async def get_summary(user_id: UserId, summary_id: uuid.UUID, summaries: Summaries) -> SummaryView:
    return await summaries.get(user_id, summary_id)


@summaries_router.post(
    "/summaries/{summary_id}/cancel", response_model=SummaryView, summary="Отменить конспект"
)
async def cancel_summary(
    user_id: UserId, summary_id: uuid.UUID, summaries: Summaries
) -> SummaryView:
    return await summaries.cancel(user_id, summary_id)


@summaries_router.post(
    "/summaries/{summary_id}/retry", response_model=SummaryView, summary="Повторить конспект"
)
async def retry_summary(
    user_id: GenerationUserId, summary_id: uuid.UUID, summaries: Summaries
) -> SummaryView:
    return await summaries.retry(user_id, summary_id)
