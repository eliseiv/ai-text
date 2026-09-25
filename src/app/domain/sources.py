"""Sources: import (PDF / pasted text / one web article), selection, deletion, retry, viewing.

Every import is accepted only after the checks that can be done synchronously (type, size,
password, page count, URL shape, topic/storage limits) and is then processed by the worker; the
row and its ingest job are committed together. Repeating a request with the same
``Idempotency-Key`` returns the SAME source (no duplicate processing); uploading the same content
again into a topic returns the existing source with ``duplicate=true``.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from functools import lru_cache
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import jobs
from app.domain.config import DomainSettings, domain_settings
from app.domain.errors import (
    DemoReadOnlyError,
    EmptyTextError,
    FileLinksNotConfiguredError,
    InvalidStateError,
    InvalidUrlError,
    LimitExceededError,
    PdfRejectedError,
    SourceDeletedError,
    SourceNotFoundError,
    TopicNotFoundError,
)
from app.domain.ingest_pdf import ExtractionError, inspect_pdf
from app.domain.ingest_web import validate_url
from app.domain.models import Source, SourceVersion, Topic
from app.domain.repository import (
    get_source,
    get_topic,
    source_view,
    touch_topic,
    version_meta,
)
from app.domain.schemas import (
    OriginalLinkView,
    PageView,
    SourceContentView,
    SourcesList,
    SourceView,
)
from app.domain.storage import LocalFileStorage, sha256_hex, sign_link, source_key
from app.domain.text import clean_text, has_meaningful_text

_MIME = {"pdf": "application/pdf", "text": "text/plain; charset=utf-8", "web": "text/html"}
_FILE_NAME = {"pdf": "original.pdf", "text": "original.txt", "web": "snapshot.html"}


@lru_cache
def get_storage() -> LocalFileStorage:
    return LocalFileStorage(domain_settings().files_dir)


def _title_from_filename(filename: str | None) -> str:
    name = os.path.basename(filename or "").strip()
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name[:200] or "Документ PDF"


def _title_from_text(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return (first[:80] + ("…" if len(first) > 80 else "")) or "Текст"


class SourceService:
    def __init__(
        self, session: AsyncSession, settings: DomainSettings, storage: LocalFileStorage
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage

    # ------------------------------------------------------------------ guards
    async def _writable_topic(self, user_id: uuid.UUID, topic_id: uuid.UUID) -> Topic:
        topic = await get_topic(self._session, user_id, topic_id)
        if topic.is_demo:
            raise DemoReadOnlyError("В демо-тему нельзя добавлять материалы. Создайте свою тему.")
        return topic

    async def _replay(self, user_id: uuid.UUID, key: str | None) -> Source | None:
        if not key:
            return None
        existing = await self._session.scalar(
            select(Source).where(Source.user_id == user_id, Source.idempotency_key == key)
        )
        if existing is not None and existing.deleted_at is not None:
            raise SourceNotFoundError("Материал не найден.")
        return existing

    async def _check_limits(self, user_id: uuid.UUID, topic_id: uuid.UUID, size: int) -> None:
        count = await self._session.scalar(
            select(func.count())
            .select_from(Source)
            .where(Source.topic_id == topic_id, Source.deleted_at.is_(None))
        )
        if int(count or 0) >= self._settings.topic_max_sources:
            raise LimitExceededError(
                f"В теме уже {self._settings.topic_max_sources} материалов — это максимум."
            )
        used = await self._session.scalar(
            select(func.coalesce(func.sum(Source.file_size), 0)).where(
                Source.user_id == user_id, Source.deleted_at.is_(None)
            )
        )
        if int(used or 0) + size > self._settings.user_storage_max_bytes:
            raise LimitExceededError("Недостаточно места в хранилище. Удалите ненужные материалы.")

    async def _duplicate(self, topic_id: uuid.UUID, sha: str) -> Source | None:
        found: Source | None = await self._session.scalar(
            select(Source).where(
                Source.topic_id == topic_id,
                Source.deleted_at.is_(None),
                Source.content_sha256 == sha,
            )
        )
        return found

    async def _insert(self, values: dict[str, Any], key: str | None) -> uuid.UUID | None:
        stmt = (
            insert(Source)
            .values(**values, idempotency_key=key)
            .on_conflict_do_nothing(
                index_elements=["user_id", "idempotency_key"],
                index_where=Source.idempotency_key.isnot(None),
            )
            .returning(Source.id)
        )
        inserted: uuid.UUID | None = await self._session.scalar(stmt)
        return inserted

    async def _finish_create(
        self, user_id: uuid.UUID, topic_id: uuid.UUID, source_id: uuid.UUID | None, key: str | None
    ) -> SourceView:
        if source_id is None:  # lost the idempotency race: return the winner
            existing = await self._replay(user_id, key)
            assert existing is not None
            return source_view(existing, replay=True)
        await jobs.enqueue(self._session, kind="ingest", ref_id=source_id, user_id=user_id)
        await touch_topic(self._session, topic_id)
        source = await get_source(self._session, user_id, source_id)
        return source_view(source)

    # ------------------------------------------------------------------ imports
    async def add_pdf(
        self,
        user_id: uuid.UUID,
        topic_id: uuid.UUID,
        *,
        filename: str | None,
        data: bytes,
        idempotency_key: str | None,
    ) -> SourceView:
        await self._writable_topic(user_id, topic_id)
        replay = await self._replay(user_id, idempotency_key)
        if replay is not None:
            return source_view(replay, replay=True)
        if len(data) > self._settings.pdf_max_bytes:
            raise LimitExceededError("Файл больше допустимого размера.")
        try:
            info = await asyncio.to_thread(
                inspect_pdf, data, max_pages=self._settings.pdf_max_pages
            )
        except ExtractionError as exc:
            raise PdfRejectedError(exc.code, exc.message) from exc
        sha = sha256_hex(data)
        duplicate = await self._duplicate(topic_id, sha)
        if duplicate is not None:
            return source_view(duplicate, duplicate=True)
        await self._check_limits(user_id, topic_id, len(data))

        source_id = uuid.uuid4()
        key = source_key(user_id, source_id, _FILE_NAME["pdf"])
        await asyncio.to_thread(self._storage.put, key, data)
        inserted = await self._insert(
            {
                "id": source_id,
                "topic_id": topic_id,
                "user_id": user_id,
                "kind": "pdf",
                "title": info.title or _title_from_filename(filename),
                "status": "extracting",
                "original_filename": os.path.basename(filename or "")[:255] or None,
                "file_key": key,
                "file_mime": _MIME["pdf"],
                "file_size": len(data),
                "content_sha256": sha,
                "page_count": info.page_count,
            },
            idempotency_key,
        )
        if inserted is None:
            self._storage.delete_source(user_id, source_id)
        return await self._finish_create(user_id, topic_id, inserted, idempotency_key)

    async def add_text(
        self,
        user_id: uuid.UUID,
        topic_id: uuid.UUID,
        *,
        title: str | None,
        text: str,
        idempotency_key: str | None,
    ) -> SourceView:
        await self._writable_topic(user_id, topic_id)
        replay = await self._replay(user_id, idempotency_key)
        if replay is not None:
            return source_view(replay, replay=True)
        cleaned = clean_text(text)
        # Empty text is never sent anywhere.
        if not has_meaningful_text(cleaned, self._settings.text_min_chars):
            raise EmptyTextError(
                f"Текст слишком короткий: нужно не меньше {self._settings.text_min_chars} символов."
            )
        if len(cleaned) > self._settings.text_max_chars:
            raise LimitExceededError(
                f"Текст длиннее {self._settings.text_max_chars} символов. Разделите его на части."
            )
        data = cleaned.encode("utf-8")
        sha = sha256_hex(data)
        duplicate = await self._duplicate(topic_id, sha)
        if duplicate is not None:
            return source_view(duplicate, duplicate=True)
        await self._check_limits(user_id, topic_id, len(data))

        source_id = uuid.uuid4()
        key = source_key(user_id, source_id, _FILE_NAME["text"])
        await asyncio.to_thread(self._storage.put, key, data)
        inserted = await self._insert(
            {
                "id": source_id,
                "topic_id": topic_id,
                "user_id": user_id,
                "kind": "text",
                "title": (title or "").strip()[:200] or _title_from_text(cleaned),
                "status": "extracting",
                "file_key": key,
                "file_mime": _MIME["text"],
                "file_size": len(data),
                "content_sha256": sha,
                "char_count": len(cleaned),
            },
            idempotency_key,
        )
        if inserted is None:
            self._storage.delete_source(user_id, source_id)
        return await self._finish_create(user_id, topic_id, inserted, idempotency_key)

    async def add_web(
        self, user_id: uuid.UUID, topic_id: uuid.UUID, *, url: str, idempotency_key: str | None
    ) -> SourceView:
        await self._writable_topic(user_id, topic_id)
        replay = await self._replay(user_id, idempotency_key)
        if replay is not None:
            return source_view(replay, replay=True)
        try:
            clean_url = validate_url(url)
        except ExtractionError as exc:
            raise InvalidUrlError(exc.message) from exc
        existing = await self._session.scalar(
            select(Source).where(
                Source.topic_id == topic_id, Source.deleted_at.is_(None), Source.url == clean_url
            )
        )
        if existing is not None:
            return source_view(existing, duplicate=True)
        await self._check_limits(user_id, topic_id, 0)
        inserted = await self._insert(
            {
                "topic_id": topic_id,
                "user_id": user_id,
                "kind": "web",
                "title": clean_url[:200],
                "status": "uploading",
                "url": clean_url,
            },
            idempotency_key,
        )
        return await self._finish_create(user_id, topic_id, inserted, idempotency_key)

    # ------------------------------------------------------------------ management
    async def list(self, user_id: uuid.UUID, topic_id: uuid.UUID) -> SourcesList:
        await get_topic(self._session, user_id, topic_id)
        rows = (
            await self._session.scalars(
                select(Source)
                .where(Source.topic_id == topic_id, Source.deleted_at.is_(None))
                .order_by(Source.created_at)
            )
        ).all()
        metas = await version_meta(self._session, [s.current_version_id for s in rows])
        ready_selected = sum(1 for s in rows if s.status == "ready" and s.selected)
        return SourcesList(
            items=[source_view(s, metas.get(s.current_version_id)) for s in rows],  # type: ignore[arg-type]
            readySelectedCount=ready_selected,
            canAsk=ready_selected > 0,
        )

    async def get(self, user_id: uuid.UUID, source_id: uuid.UUID) -> SourceView:
        source = await get_source(self._session, user_id, source_id)
        metas = await version_meta(self._session, [source.current_version_id])
        return source_view(source, metas.get(source.current_version_id))  # type: ignore[arg-type]

    async def update(
        self,
        user_id: uuid.UUID,
        source_id: uuid.UUID,
        *,
        selected: bool | None,
        title: str | None,
    ) -> SourceView:
        source = await get_source(self._session, user_id, source_id)
        if title is not None:
            topic = await get_topic(self._session, user_id, source.topic_id)
            if topic.is_demo:
                raise DemoReadOnlyError("Демо-тему нельзя изменить.")
            source.title = title.strip()
        if selected is not None:
            source.selected = selected
        source.updated_at = datetime.datetime.now(tz=datetime.UTC)
        await self._session.flush()
        return await self.get(user_id, source_id)

    async def delete(self, user_id: uuid.UUID, source_id: uuid.UUID) -> None:
        source = await get_source(self._session, user_id, source_id)
        topic = await get_topic(self._session, user_id, source.topic_id)
        if topic.is_demo:
            raise DemoReadOnlyError("Из демо-темы нельзя удалять материалы.")
        # Excluded from every new answer at once; files/versions/chunks purged by the worker.
        await self._session.execute(
            update(Source).where(Source.id == source.id).values(deleted_at=func.now())
        )
        await jobs.cancel_queued(self._session, kind="ingest", ref_id=source.id)
        await jobs.enqueue(
            self._session,
            kind="purge_source",
            ref_id=source.id,
            user_id=user_id,
            delay_seconds=self._settings.purge_delay_seconds,
        )
        await touch_topic(self._session, source.topic_id)

    async def retry(self, user_id: uuid.UUID, source_id: uuid.UUID) -> SourceView:
        source = await get_source(self._session, user_id, source_id)
        if source.status != "failed":
            raise InvalidStateError("Повторить можно только материал с ошибкой.")
        source.status = "uploading" if source.kind == "web" else "extracting"
        source.error_code = None
        source.error_message = None
        source.updated_at = datetime.datetime.now(tz=datetime.UTC)
        await self._session.flush()
        await jobs.enqueue(self._session, kind="ingest", ref_id=source.id, user_id=user_id)
        return source_view(source)

    # ------------------------------------------------------------------ viewing
    async def _owned_including_deleted(self, user_id: uuid.UUID, source_id: uuid.UUID) -> Source:
        row = (
            await self._session.execute(
                select(Source, Topic.deleted_at)
                .join(Topic, Topic.id == Source.topic_id)
                .where(Source.id == source_id, Source.user_id == user_id)
            )
        ).first()
        if row is None or row[1] is not None:
            raise (
                TopicNotFoundError("Тема не найдена.")
                if row
                else SourceNotFoundError("Материал не найден.")
            )
        source: Source = row[0]
        if source.deleted_at is not None:
            raise SourceDeletedError("Источник удалён.")
        return source

    async def content(
        self,
        user_id: uuid.UUID,
        source_id: uuid.UUID,
        *,
        version_id: uuid.UUID | None,
        offset: int,
        limit: int,
    ) -> SourceContentView:
        source = await self._owned_including_deleted(user_id, source_id)
        wanted = version_id or source.current_version_id
        if wanted is None:
            raise InvalidStateError("Текст материала ещё не готов.")
        version = await self._session.scalar(
            select(SourceVersion).where(
                SourceVersion.id == wanted, SourceVersion.source_id == source.id
            )
        )
        if version is None:
            raise SourceDeletedError("Эта версия источника больше не доступна.")
        meta = version.meta or {}
        start = max(0, min(offset, version.char_count))
        return SourceContentView(
            sourceId=source.id,
            versionId=version.id,
            isCurrentVersion=version.id == source.current_version_id,
            kind=source.kind,
            title=source.title,
            text=version.text[start : start + limit],
            offset=start,
            totalChars=version.char_count,
            pages=[
                PageView(number=p["n"], label=p.get("label"), start=p["start"], end=p["end"])
                for p in (version.pages or [])
            ],
            pagesWithoutText=list(meta.get("pagesWithoutText", [])),
            url=meta.get("url") or source.url,
            fetchedAt=meta.get("fetchedAt"),
        )

    async def original_link(
        self, user_id: uuid.UUID, source_id: uuid.UUID, base_url: str
    ) -> OriginalLinkView:
        source = await self._owned_including_deleted(user_id, source_id)
        if not source.file_key:
            raise InvalidStateError("Оригинал ещё не сохранён.")
        if not self._settings.files_signing_secret:
            raise FileLinksNotConfiguredError("Ссылки на файлы не настроены на сервере.")
        token, expires = sign_link(
            self._settings.files_signing_secret,
            source_id=source.id,
            user_id=user_id,
            ttl_seconds=self._settings.file_link_ttl_seconds,
        )
        return OriginalLinkView(
            url=f"{base_url.rstrip('/')}/v1/files/{token}",
            expiresAt=datetime.datetime.fromtimestamp(expires, tz=datetime.UTC),
            mime=source.file_mime or "application/octet-stream",
            filename=source.original_filename,
        )
