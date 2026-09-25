"""Topics: create / list+search / rename / delete (soft now, purge later)."""

from __future__ import annotations

import base64
import datetime
import json
import uuid

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import jobs
from app.domain.config import DomainSettings
from app.domain.errors import DemoReadOnlyError, LimitExceededError, TopicNotFoundError
from app.domain.models import Source, Topic
from app.domain.repository import get_topic, now, topic_counts, topic_view
from app.domain.schemas import TopicsPage, TopicView

DEFAULT_TITLE = "Новая тема"


def _encode_cursor(updated_at: datetime.datetime, topic_id: uuid.UUID) -> str:
    raw = json.dumps({"u": updated_at.isoformat(), "i": str(topic_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime.datetime, uuid.UUID] | None:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.datetime.fromisoformat(raw["u"]), uuid.UUID(raw["i"])
    except (ValueError, KeyError, TypeError):
        return None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TopicService:
    def __init__(self, session: AsyncSession, settings: DomainSettings) -> None:
        self._session = session
        self._settings = settings

    async def create(
        self, user_id: uuid.UUID, title: str | None, idempotency_key: str | None
    ) -> TopicView:
        if idempotency_key:
            existing = await self._session.scalar(
                select(Topic).where(
                    Topic.user_id == user_id, Topic.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.deleted_at is not None:
                    raise TopicNotFoundError("Тема не найдена.")
                counts = await topic_counts(self._session, [existing.id])
                return topic_view(existing, counts.get(existing.id, (0, 0)), replay=True)
        count = await self._session.scalar(
            select(func.count())
            .select_from(Topic)
            .where(Topic.user_id == user_id, Topic.deleted_at.is_(None), Topic.is_demo.is_(False))
        )
        if int(count or 0) >= self._settings.user_max_topics:
            raise LimitExceededError(
                f"Достигнут лимит тем: {self._settings.user_max_topics}. Удалите ненужные темы."
            )
        clean = (title or "").strip()
        stmt = (
            insert(Topic)
            .values(
                user_id=user_id,
                title=clean or DEFAULT_TITLE,
                title_is_auto=not clean,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "idempotency_key"],
                index_where=Topic.idempotency_key.isnot(None),
            )
            .returning(Topic.id)
        )
        topic_id = await self._session.scalar(stmt)
        if topic_id is None:  # a concurrent request with the same key won
            topic = await self._session.scalar(
                select(Topic).where(
                    Topic.user_id == user_id, Topic.idempotency_key == idempotency_key
                )
            )
            assert topic is not None
            return topic_view(topic, (0, 0), replay=True)
        topic = await get_topic(self._session, user_id, topic_id)
        return topic_view(topic, (0, 0))

    async def list(
        self, user_id: uuid.UUID, *, query: str | None, cursor: str | None, limit: int
    ) -> TopicsPage:
        stmt = select(Topic).where(Topic.user_id == user_id, Topic.deleted_at.is_(None))
        if query and query.strip():
            pattern = f"%{_escape_like(query.strip())}%"
            # Search by topic title OR by the title of a material inside it.
            stmt = stmt.where(
                or_(
                    Topic.title.ilike(pattern, escape="\\"),
                    Topic.id.in_(
                        select(Source.topic_id).where(
                            Source.user_id == user_id,
                            Source.deleted_at.is_(None),
                            Source.title.ilike(pattern, escape="\\"),
                        )
                    ),
                )
            )
        decoded = _decode_cursor(cursor) if cursor else None
        if decoded is not None:
            updated_at, topic_id = decoded
            stmt = stmt.where(
                or_(
                    Topic.updated_at < updated_at,
                    and_(Topic.updated_at == updated_at, Topic.id < topic_id),
                )
            )
        rows = (
            await self._session.scalars(
                stmt.order_by(Topic.updated_at.desc(), Topic.id.desc()).limit(limit + 1)
            )
        ).all()
        page = rows[:limit]
        counts = await topic_counts(self._session, [t.id for t in page])
        next_cursor = (
            _encode_cursor(page[-1].updated_at, page[-1].id) if len(rows) > limit and page else None
        )
        return TopicsPage(
            items=[topic_view(t, counts.get(t.id, (0, 0))) for t in page], nextCursor=next_cursor
        )

    async def get(self, user_id: uuid.UUID, topic_id: uuid.UUID) -> TopicView:
        topic = await get_topic(self._session, user_id, topic_id)
        counts = await topic_counts(self._session, [topic.id])
        return topic_view(topic, counts.get(topic.id, (0, 0)))

    async def rename(self, user_id: uuid.UUID, topic_id: uuid.UUID, title: str) -> TopicView:
        topic = await get_topic(self._session, user_id, topic_id)
        if topic.is_demo:
            raise DemoReadOnlyError("Демо-тему нельзя изменить.")
        topic.title = title.strip()
        topic.title_is_auto = False
        topic.updated_at = now()
        await self._session.flush()
        return await self.get(user_id, topic_id)

    async def delete(self, user_id: uuid.UUID, topic_id: uuid.UUID) -> None:
        topic = await get_topic(self._session, user_id, topic_id)
        await soft_delete_topics(self._session, [topic.id], user_id, self._settings)


async def soft_delete_topics(
    session: AsyncSession,
    topic_ids: list[uuid.UUID],
    user_id: uuid.UUID,
    settings: DomainSettings,
) -> None:
    """Invisible immediately (every read filters ``deleted_at``); purged after the delay."""
    if not topic_ids:
        return
    await session.execute(
        update(Topic)
        .where(Topic.id.in_(topic_ids), Topic.user_id == user_id)
        .values(deleted_at=func.now())
    )
    for topic_id in topic_ids:
        await jobs.enqueue(
            session,
            kind="purge_topic",
            ref_id=topic_id,
            user_id=user_id,
            delay_seconds=settings.purge_delay_seconds,
        )
