"""Topics: «Мои темы» — create, list/search, rename, delete."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.deps import DbSession
from app.domain.demo import ensure_demo
from app.domain.routers.deps import IdempotencyKey, Settings, Topics, UserId
from app.domain.schemas import TopicCreate, TopicsPage, TopicUpdate, TopicView
from app.domain.sources import get_storage

router = APIRouter(prefix="/v1/topics", tags=["Topics"])


@router.post(
    "",
    response_model=TopicView,
    status_code=201,
    summary="Создать тему",
    description="Без `title` сервер назовёт тему по первому готовому материалу (`titleIsAuto`).",
)
async def create_topic(
    user_id: UserId, body: TopicCreate, topics: Topics, key: IdempotencyKey
) -> TopicView:
    return await topics.create(user_id, body.title, key)


@router.get(
    "",
    response_model=TopicsPage,
    summary="Мои темы",
    description=(
        "Темы пользователя, новые изменения сверху. `query` ищет по названию темы и названиям "
        "материалов. Демо-тема создаётся при первом запросе и помечена `isDemo`."
    ),
)
async def list_topics(
    user_id: UserId,
    topics: Topics,
    session: DbSession,
    settings: Settings,
    query: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> TopicsPage:
    if settings.demo_enabled and cursor is None:
        await ensure_demo(session, user_id, get_storage())
    return await topics.list(user_id, query=query, cursor=cursor, limit=limit)


@router.get("/{topic_id}", response_model=TopicView, summary="Тема")
async def get_topic(user_id: UserId, topic_id: uuid.UUID, topics: Topics) -> TopicView:
    return await topics.get(user_id, topic_id)


@router.patch("/{topic_id}", response_model=TopicView, summary="Переименовать тему")
async def rename_topic(
    user_id: UserId, topic_id: uuid.UUID, body: TopicUpdate, topics: Topics
) -> TopicView:
    return await topics.rename(user_id, topic_id, body.title)


@router.delete(
    "/{topic_id}",
    status_code=204,
    response_class=Response,
    summary="Удалить тему",
    description=(
        "Тема сразу исчезает из всех ответов API (старые ссылки — `404`). Файлы, индексы, "
        "история и конспекты удаляются фоновой задачей через `PURGE_DELAY_SECONDS`."
    ),
)
async def delete_topic(user_id: UserId, topic_id: uuid.UUID, topics: Topics) -> Response:
    await topics.delete(user_id, topic_id)
    return Response(status_code=204)
