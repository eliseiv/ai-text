"""Owner-scoped reads + row→view mapping.

EVERY fetch here takes ``user_id`` and filters on it. A foreign or deleted id is a plain 404 —
indistinguishable from a missing one. Deleted topics/sources vanish from every read immediately;
the purge job removes the data afterwards.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import (
    MessageNotFoundError,
    NoReadySourcesError,
    NoSourcesSelectedError,
    SourceNotFoundError,
    SummaryNotFoundError,
    TopicNotFoundError,
)
from app.domain.models import ChatMessage, Source, SourceVersion, Summary, Topic
from app.domain.schemas import (
    AnswerView,
    CitationView,
    ErrorView,
    MessageView,
    SkippedSourceView,
    SourceView,
    SummaryView,
    TopicView,
    UsageView,
    UsedSourceView,
)


def now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


# --- fetchers ----------------------------------------------------------------------------------
async def get_topic(session: AsyncSession, user_id: uuid.UUID, topic_id: uuid.UUID) -> Topic:
    topic = await session.scalar(
        select(Topic).where(
            Topic.id == topic_id, Topic.user_id == user_id, Topic.deleted_at.is_(None)
        )
    )
    if topic is None:
        raise TopicNotFoundError("Тема не найдена.")
    return topic


async def get_source(
    session: AsyncSession, user_id: uuid.UUID, source_id: uuid.UUID, *, with_topic: bool = True
) -> Source:
    stmt = select(Source).where(
        Source.id == source_id, Source.user_id == user_id, Source.deleted_at.is_(None)
    )
    if with_topic:
        stmt = stmt.join(Topic, Topic.id == Source.topic_id).where(Topic.deleted_at.is_(None))
    source = await session.scalar(stmt)
    if source is None:
        raise SourceNotFoundError("Материал не найден.")
    return source


async def get_message(
    session: AsyncSession, user_id: uuid.UUID, topic_id: uuid.UUID, message_id: uuid.UUID
) -> ChatMessage:
    message = await session.scalar(
        select(ChatMessage).where(
            ChatMessage.id == message_id,
            ChatMessage.topic_id == topic_id,
            ChatMessage.user_id == user_id,
        )
    )
    if message is None:
        raise MessageNotFoundError("Сообщение не найдено.")
    return message


async def get_summary(session: AsyncSession, user_id: uuid.UUID, summary_id: uuid.UUID) -> Summary:
    summary = await session.scalar(
        select(Summary)
        .join(Topic, Topic.id == Summary.topic_id)
        .where(Summary.id == summary_id, Summary.user_id == user_id, Topic.deleted_at.is_(None))
    )
    if summary is None:
        raise SummaryNotFoundError("Конспект не найден.")
    return summary


async def touch_topic(session: AsyncSession, topic_id: uuid.UUID) -> None:
    await session.execute(update(Topic).where(Topic.id == topic_id).values(updated_at=func.now()))


# --- selection ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Selection:
    usable: list[Source]
    skipped: list[SkippedSourceView]

    @property
    def version_ids(self) -> list[uuid.UUID]:
        return [s.current_version_id for s in self.usable if s.current_version_id is not None]

    @property
    def source_ids(self) -> list[uuid.UUID]:
        return [s.id for s in self.usable]


async def resolve_selection(
    session: AsyncSession,
    user_id: uuid.UUID,
    topic_id: uuid.UUID,
    requested: Sequence[uuid.UUID] | None,
) -> Selection:
    """Which sources an answer/summary may use RIGHT NOW.

    Only ready, selected, non-deleted sources of THIS topic. An explicit ``requested`` list narrows
    that set (it can never widen it: an excluded or deleted material is not used, even when an
    old message asks for it). Not-ready ones are reported as skipped — the answer proceeds with
    the ready ones.
    """
    rows = (
        await session.scalars(
            select(Source)
            .where(Source.topic_id == topic_id, Source.user_id == user_id)
            .order_by(Source.created_at)
        )
    ).all()
    by_id = {s.id: s for s in rows}
    wanted: Iterable[Source]
    skipped: list[SkippedSourceView] = []
    if requested is None:
        wanted = [s for s in rows if s.deleted_at is None and s.selected]
    else:
        wanted = []
        for sid in dict.fromkeys(requested):
            source = by_id.get(sid)
            if source is None or source.deleted_at is not None:
                if source is not None:
                    skipped.append(
                        SkippedSourceView(sourceId=sid, title=source.title, reason="deleted")
                    )
                continue
            if not source.selected:
                skipped.append(
                    SkippedSourceView(sourceId=sid, title=source.title, reason="not_selected")
                )
                continue
            wanted.append(source)
    usable = []
    for source in wanted:
        if source.status == "ready" and source.current_version_id is not None:
            usable.append(source)
        else:
            reason = "failed" if source.status == "failed" else "not_ready"
            skipped.append(SkippedSourceView(sourceId=source.id, title=source.title, reason=reason))
    if not usable:
        if not wanted:
            raise NoSourcesSelectedError(
                "Не выбрано ни одного материала. Отметьте хотя бы один готовый материал."
            )
        raise NoReadySourcesError(
            "Выбранные материалы ещё обрабатываются или не загрузились. Дождитесь готовности."
        )
    return Selection(usable=usable, skipped=skipped)


# --- views -------------------------------------------------------------------------------------
async def topic_counts(
    session: AsyncSession, topic_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    if not topic_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Source.topic_id,
                func.count(),
                func.count().filter(Source.status == "ready"),
            )
            .where(Source.topic_id.in_(topic_ids), Source.deleted_at.is_(None))
            .group_by(Source.topic_id)
        )
    ).all()
    return {r[0]: (int(r[1]), int(r[2])) for r in rows}


def topic_view(topic: Topic, counts: tuple[int, int], *, replay: bool = False) -> TopicView:
    return TopicView(
        id=topic.id,
        title=topic.title,
        titleIsAuto=topic.title_is_auto,
        isDemo=topic.is_demo,
        sourcesCount=counts[0],
        readySourcesCount=counts[1],
        createdAt=topic.created_at,
        updatedAt=topic.updated_at,
        idempotentReplay=replay,
    )


_SUGGESTIONS = {
    "web_unavailable": "paste_text",
    "empty_text": "paste_text",
    "web_unsupported_content": "paste_text",
    "internal_error": "retry",
    "timeout": "retry",
}


def error_view(code: str | None, message: str | None) -> ErrorView | None:
    if not code:
        return None
    return ErrorView(
        code=code, message=message or "Произошла ошибка.", suggestion=_SUGGESTIONS.get(code)
    )


async def version_meta(
    session: AsyncSession, version_ids: Sequence[uuid.UUID | None]
) -> dict[uuid.UUID, dict[str, Any]]:
    ids = [v for v in version_ids if v is not None]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(SourceVersion.id, SourceVersion.meta).where(SourceVersion.id.in_(ids))
        )
    ).all()
    return {r[0]: r[1] or {} for r in rows}


def source_view(
    source: Source,
    meta: dict[str, Any] | None = None,
    *,
    duplicate: bool = False,
    replay: bool = False,
) -> SourceView:
    meta = meta or {}
    return SourceView(
        id=source.id,
        topicId=source.topic_id,
        kind=source.kind,
        title=source.title,
        status=source.status,
        selected=source.selected,
        error=error_view(source.error_code, source.error_message),
        url=source.url,
        originalFilename=source.original_filename,
        fileSize=int(source.file_size or 0),
        pageCount=source.page_count,
        charCount=source.char_count,
        pagesWithoutText=list(meta.get("pagesWithoutText", [])),
        currentVersionId=source.current_version_id,
        fetchedAt=meta.get("fetchedAt"),
        createdAt=source.created_at,
        updatedAt=source.updated_at,
        duplicate=duplicate,
        idempotentReplay=replay,
    )


async def _source_states(
    session: AsyncSession, source_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, bool]]:
    ids = list(dict.fromkeys(source_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Source.id, Source.title, Source.deleted_at).where(Source.id.in_(ids))
        )
    ).all()
    return {r[0]: (r[1], r[2] is not None) for r in rows}


def _citations(
    raw: list[dict[str, Any]], states: dict[uuid.UUID, tuple[str, bool]]
) -> list[CitationView]:
    result = []
    for c in raw:
        sid = uuid.UUID(str(c["sourceId"]))
        state = states.get(sid)
        result.append(CitationView(**{**c, "sourceDeleted": state is None or state[1]}))
    return result


async def _used_sources(
    session: AsyncSession, source_ids: Sequence[uuid.UUID], version_ids: Sequence[uuid.UUID]
) -> tuple[list[UsedSourceView], dict[uuid.UUID, tuple[str, bool]]]:
    rows = (
        (
            await session.execute(
                select(SourceVersion.id, SourceVersion.source_id).where(
                    SourceVersion.id.in_(version_ids)
                )
            )
        ).all()
        if version_ids
        else []
    )
    version_of = {r[1]: r[0] for r in rows}
    states = await _source_states(session, [*source_ids, *version_of.keys()])
    used = []
    for sid in dict.fromkeys([*version_of.keys(), *source_ids]):
        title, deleted = states.get(sid, ("Источник удалён", True))
        used.append(
            UsedSourceView(
                sourceId=sid, versionId=version_of.get(sid), title=title, deleted=deleted
            )
        )
    return used, states


async def message_view(
    session: AsyncSession, message: ChatMessage, *, replay: bool = False
) -> MessageView:
    used, states = await _used_sources(
        session, message.requested_source_ids, message.source_version_ids
    )
    answer = None
    skipped: list[SkippedSourceView] = []
    if message.answer:
        raw = message.answer
        skipped = [SkippedSourceView(**s) for s in raw.get("skippedSources", [])]
        if "text" in raw:
            answer = AnswerView(
                status=raw["status"],
                text=raw["text"],
                blocks=raw.get("blocks", []),
                citations=_citations(raw.get("citations", []), states),
                partialContext=bool(raw.get("partialContext", False)),
            )
    usage = message.usage or {}
    return MessageView(
        id=message.id,
        topicId=message.topic_id,
        question=message.question,
        status=message.status,
        answer=answer,
        sources=used,
        sourceVersionIds=list(message.source_version_ids),
        skippedSources=skipped,
        blockReason=message.block_reason,
        error=error_view(message.error_code, message.error_message),
        usage=UsageView(
            creditsCharged=int(usage.get("creditsCharged", 0)), free=bool(usage.get("free", False))
        ),
        createdAt=message.created_at,
        completedAt=message.completed_at,
        idempotentReplay=replay,
    )


async def summary_view(
    session: AsyncSession,
    summary: Summary,
    *,
    current_version_ids: Sequence[uuid.UUID] | None = None,
    replay: bool = False,
    reused: bool = False,
) -> SummaryView:
    used, states = await _used_sources(
        session, summary.requested_source_ids, summary.source_version_ids
    )
    content = summary.content or {}
    stale = False
    if current_version_ids is not None and summary.status == "succeeded":
        stale = set(current_version_ids) != set(summary.source_version_ids)
    stale = stale or any(u.deleted for u in used)
    usage = summary.usage or {}
    return SummaryView(
        id=summary.id,
        topicId=summary.topic_id,
        status=summary.status,
        theses=content.get("theses", []),
        questions=content.get("questions", []),
        citations=_citations(content.get("citations", []), states),
        coverage=content.get("coverage"),
        sources=used,
        skippedSources=[SkippedSourceView(**s) for s in content.get("skippedSources", [])],
        stale=stale,
        reused=reused,
        blockReason=summary.block_reason,
        error=error_view(summary.error_code, summary.error_message),
        usage=UsageView(
            creditsCharged=int(usage.get("creditsCharged", 0)), free=bool(usage.get("free", False))
        ),
        createdAt=summary.created_at,
        completedAt=summary.completed_at,
        idempotentReplay=replay,
    )
