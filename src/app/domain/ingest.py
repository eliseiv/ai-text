"""Ingest handler: original → text version → chunks (+ full-text index) → ``ready``.

Status transitions are committed as they happen (``uploading`` → ``extracting`` → ``indexing``),
so a client coming back from background sees the real stage. The version and ALL its chunks are
written in one transaction together with ``ready``: a source is never ``ready`` with a partial
index, and a crash mid-way leaves no half-written version (the job is simply re-run).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.config import DomainSettings
from app.domain.ingest_pdf import ExtractedDoc, ExtractionError, extract_pdf
from app.domain.ingest_web import WebFetcher, extract_article
from app.domain.models import Chunk, Source, SourceVersion, Topic
from app.domain.storage import LocalFileStorage, sha256_hex, source_key
from app.domain.text import chunk_text, clean_text
from app.observability.logging import log_event

logger = logging.getLogger("app.domain.ingest")


class RetryableIngestError(Exception):
    """Transient (network, 5xx): the job is retried with backoff before the source fails."""


async def _set_status(session: AsyncSession, source_id: uuid.UUID, status: str) -> None:
    await session.execute(
        update(Source).where(Source.id == source_id).values(status=status, updated_at=func.now())
    )
    await session.commit()


async def mark_failed(session: AsyncSession, source_id: uuid.UUID, code: str, message: str) -> None:
    await session.execute(
        update(Source)
        .where(Source.id == source_id)
        .values(status="failed", error_code=code, error_message=message, updated_at=func.now())
    )
    await session.commit()


async def run_ingest(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    settings: DomainSettings,
    storage: LocalFileStorage,
    fetcher: WebFetcher,
    is_last_attempt: bool,
) -> None:
    source = await session.scalar(select(Source).where(Source.id == source_id))
    if source is None or source.deleted_at is not None or source.status in ("ready", "failed"):
        return
    topic = await session.scalar(select(Topic).where(Topic.id == source.topic_id))
    if topic is None or topic.deleted_at is not None:
        return
    await session.execute(
        update(Source).where(Source.id == source_id).values(attempts=Source.attempts + 1)
    )
    await session.commit()

    try:
        doc = await _extract(session, source, settings=settings, storage=storage, fetcher=fetcher)
    except ExtractionError as exc:
        if exc.retryable and not is_last_attempt:
            raise RetryableIngestError(exc.code) from exc
        await mark_failed(session, source_id, exc.code, exc.message)
        log_event(logger, logging.INFO, "ingest_rejected", sourceId=str(source_id), code=exc.code)
        return

    await _set_status(session, source_id, "indexing")
    spans = chunk_text(doc.text, settings.chunk_target_chars)
    await _write_version(session, source, doc, spans, settings)
    log_event(
        logger,
        logging.INFO,
        "ingest_ready",
        sourceId=str(source_id),
        kind=source.kind,
        chars=len(doc.text),
        chunks=len(spans),
    )


async def _extract(
    session: AsyncSession,
    source: Source,
    *,
    settings: DomainSettings,
    storage: LocalFileStorage,
    fetcher: WebFetcher,
) -> ExtractedDoc:
    if source.kind == "web":
        await _set_status(session, source.id, "uploading")
        page = await fetcher.fetch(source.url or "")
        key = source_key(source.user_id, source.id, "snapshot.html")
        await asyncio.to_thread(storage.put, key, page.body)
        await session.execute(
            update(Source)
            .where(Source.id == source.id)
            .values(
                file_key=key,
                file_mime=page.content_type,
                file_size=len(page.body),
                status="extracting",
                updated_at=func.now(),
            )
        )
        await session.commit()
        doc = await asyncio.to_thread(extract_article, page)
        return doc

    await _set_status(session, source.id, "extracting")
    if not source.file_key:
        raise ExtractionError("internal_error", "Файл материала не найден. Загрузите его снова.")
    data = await asyncio.to_thread(storage.read, source.file_key)
    if source.kind == "pdf":
        return await asyncio.to_thread(
            extract_pdf,
            data,
            max_pages=settings.pdf_max_pages,
            min_chars_per_page=settings.pdf_min_chars_per_page,
            min_text_page_ratio=settings.pdf_min_text_page_ratio,
        )
    text = clean_text(data.decode("utf-8"))
    return ExtractedDoc(text=text)


async def _write_version(
    session: AsyncSession,
    source: Source,
    doc: ExtractedDoc,
    spans: list[Any],
    settings: DomainSettings,
) -> None:
    version_no = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(SourceVersion.version_no), 0)).where(
                    SourceVersion.source_id == source.id
                )
            )
            or 0
        )
        + 1
    )
    version_id = await session.scalar(
        insert(SourceVersion)
        .values(
            source_id=source.id,
            user_id=source.user_id,
            version_no=version_no,
            text=doc.text,
            char_count=len(doc.text),
            text_sha256=sha256_hex(doc.text.encode("utf-8")),
            pages=doc.pages,
            meta=doc.meta,
        )
        .returning(SourceVersion.id)
    )
    if spans:
        await session.execute(
            insert(Chunk),
            [
                {
                    "version_id": version_id,
                    "source_id": source.id,
                    "user_id": source.user_id,
                    "ordinal": s.ordinal,
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                }
                for s in spans
            ],
        )
    values: dict[str, Any] = {
        "status": "ready",
        "current_version_id": version_id,
        "char_count": len(doc.text),
        "error_code": None,
        "error_message": None,
        "updated_at": func.now(),
    }
    if doc.pages is not None:
        values["page_count"] = len(doc.pages)
    if (
        doc.title
        and source.kind in ("pdf", "web")
        and (source.kind == "web" or source.title.startswith("Документ"))
    ):
        values["title"] = doc.title
    await session.execute(update(Source).where(Source.id == source.id).values(**values))

    # Auto title: an auto-named topic takes the title of its first ready material.
    title = values.get("title", source.title)
    await session.execute(
        update(Topic)
        .where(Topic.id == source.topic_id, Topic.title_is_auto.is_(True))
        .values(title=str(title)[:200], title_is_auto=False, updated_at=func.now())
    )
    await session.execute(
        update(Topic).where(Topic.id == source.topic_id).values(updated_at=func.now())
    )
    await session.commit()
