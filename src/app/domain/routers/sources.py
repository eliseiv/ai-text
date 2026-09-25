"""Sources: import (PDF / text / web link), list, select, delete, retry, view original."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, Request, Response, UploadFile

from app.domain.errors import LimitExceededError, UnsupportedFileError
from app.domain.routers.deps import IdempotencyKey, Settings, Sources, UserId, public_base_url
from app.domain.schemas import (
    OriginalLinkView,
    SourceContentView,
    SourcesList,
    SourceTextCreate,
    SourceUpdate,
    SourceView,
    SourceWebCreate,
)

router = APIRouter(prefix="/v1", tags=["Sources"])

_PDF_TYPES = {"application/pdf", "application/x-pdf", "application/octet-stream", ""}
_READ_CHUNK = 1024 * 1024


@router.post(
    "/topics/{topic_id}/sources/pdf",
    response_model=SourceView,
    status_code=201,
    summary="Загрузить PDF",
    description=(
        "`multipart/form-data`, поле `file`. Сразу проверяются тип, размер, повреждение, пароль и "
        "число страниц (`422` с кодом: `unsupported_file`, `limit_exceeded`, `pdf_corrupted`, "
        "`pdf_password_protected`, `pdf_too_many_pages`). Текстовый слой проверяется при "
        "обработке: скан без текста получит `failed` с кодом `pdf_no_text_layer`. Лимиты — "
        "`GET /v1/limits`."
    ),
)
async def upload_pdf(
    user_id: UserId,
    topic_id: uuid.UUID,
    sources: Sources,
    settings: Settings,
    key: IdempotencyKey,
    file: Annotated[UploadFile, File(description="PDF-файл")],
) -> SourceView:
    if (file.content_type or "").split(";")[0].strip().lower() not in _PDF_TYPES:
        raise UnsupportedFileError("Поддерживаются только PDF-файлы.")
    data = bytearray()
    while piece := await file.read(_READ_CHUNK):
        data.extend(piece)
        if len(data) > settings.pdf_max_bytes:
            raise LimitExceededError(f"Файл больше {settings.pdf_max_bytes // (1024 * 1024)} МБ.")
    return await sources.add_pdf(
        user_id, topic_id, filename=file.filename, data=bytes(data), idempotency_key=key
    )


@router.post(
    "/topics/{topic_id}/sources/text",
    response_model=SourceView,
    status_code=201,
    summary="Добавить текст",
    description=(
        "Пустой или слишком короткий текст отклоняется (`empty_text`) и никуда не отправляется."
    ),
)
async def add_text(
    user_id: UserId,
    topic_id: uuid.UUID,
    body: SourceTextCreate,
    sources: Sources,
    key: IdempotencyKey,
) -> SourceView:
    return await sources.add_text(
        user_id, topic_id, title=body.title, text=body.text, idempotency_key=key
    )


@router.post(
    "/topics/{topic_id}/sources/web",
    response_model=SourceView,
    status_code=201,
    summary="Добавить веб-статью",
    description=(
        "Одна публичная страница: сервер скачивает её, сохраняет снимок (URL и дату) и извлекает "
        "основной текст. Сайт не обходится, внутренние адреса не загружаются (`url_not_public`). "
        "Если статья недоступна, источник получит `failed` с `suggestion: paste_text`."
    ),
)
async def add_web(
    user_id: UserId,
    topic_id: uuid.UUID,
    body: SourceWebCreate,
    sources: Sources,
    key: IdempotencyKey,
) -> SourceView:
    return await sources.add_web(user_id, topic_id, url=body.url, idempotency_key=key)


@router.get(
    "/topics/{topic_id}/sources",
    response_model=SourcesList,
    summary="Материалы темы",
    description="Статусы восстанавливаются после сворачивания приложения: опрашивайте этот список.",
)
async def list_sources(user_id: UserId, topic_id: uuid.UUID, sources: Sources) -> SourcesList:
    return await sources.list(user_id, topic_id)


@router.get("/sources/{source_id}", response_model=SourceView, summary="Материал")
async def get_source(user_id: UserId, source_id: uuid.UUID, sources: Sources) -> SourceView:
    return await sources.get(user_id, source_id)


@router.patch(
    "/sources/{source_id}",
    response_model=SourceView,
    summary="Выбрать / переименовать материал",
    description="`selected=false` исключает материал из новых ответов и конспектов.",
)
async def update_source(
    user_id: UserId, source_id: uuid.UUID, body: SourceUpdate, sources: Sources
) -> SourceView:
    return await sources.update(user_id, source_id, selected=body.selected, title=body.title)


@router.delete(
    "/sources/{source_id}",
    status_code=204,
    response_class=Response,
    summary="Удалить материал",
    description="Сразу исключается из новых ответов; старые цитаты показывают `sourceDeleted`.",
)
async def delete_source(user_id: UserId, source_id: uuid.UUID, sources: Sources) -> Response:
    await sources.delete(user_id, source_id)
    return Response(status_code=204)


@router.post(
    "/sources/{source_id}/retry",
    response_model=SourceView,
    summary="Повторить обработку",
    description="Только для `failed`. Повтор не создаёт новый материал.",
)
async def retry_source(user_id: UserId, source_id: uuid.UUID, sources: Sources) -> SourceView:
    return await sources.retry(user_id, source_id)


@router.get(
    "/sources/{source_id}/content",
    response_model=SourceContentView,
    summary="Текст источника",
    description=(
        "Текст версии (по умолчанию — текущей) окном `[offset, offset+limit)`. Смещения цитат "
        "(`citations[].offsets`) и страницы (`pages`) — в тех же координатах. Удалённый источник — "
        "`410 source_deleted`."
    ),
)
async def source_content(
    user_id: UserId,
    source_id: uuid.UUID,
    sources: Sources,
    versionId: Annotated[uuid.UUID | None, Query()] = None,  # noqa: N803
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500_000)] = 500_000,
) -> SourceContentView:
    return await sources.content(
        user_id, source_id, version_id=versionId, offset=offset, limit=limit
    )


@router.get(
    "/sources/{source_id}/original",
    response_model=OriginalLinkView,
    summary="Ссылка на оригинал",
    description="Временная ссылка на файл (PDF, текст или снимок веб-страницы).",
)
async def original_link(
    user_id: UserId, source_id: uuid.UUID, sources: Sources, settings: Settings, request: Request
) -> OriginalLinkView:
    return await sources.original_link(user_id, source_id, public_base_url(request, settings))
