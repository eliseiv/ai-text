"""Summaries (конспект) of the selected ready sources of a topic.

Same lifecycle and money rules as answers (see ``chat.py``). One extra rule: if a summary over
EXACTLY the same source versions already succeeded, it is returned (``reused=true``) instead of
being generated and charged again.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import access, jobs
from app.domain.config import DomainSettings, domain_settings
from app.domain.errors import (
    InvalidStateError,
    NoReadySourcesError,
    NoSourcesSelectedError,
    SummaryNotFoundError,
)
from app.domain.models import Chunk, Summary
from app.domain.pricing import KIND_SUMMARY, summary_parts, summary_price
from app.domain.repository import (
    get_summary,
    get_topic,
    now,
    resolve_selection,
    summary_view,
    touch_topic,
)
from app.domain.schemas import SummaryPriceView, SummaryView


async def summary_quote(
    session: AsyncSession, version_ids: list[uuid.UUID]
) -> tuple[int, int, int]:
    """``(credits, parts_read, parts_total)`` of a summary over these versions.

    Computed from the stored chunk sizes with the SAME split the run uses, so the price shown and
    checked before the run is the price the run charges.
    """
    from app.domain.provider import plan_summary_parts

    order = {v: i for i, v in enumerate(version_ids)}
    rows = (
        (
            await session.execute(
                select(Chunk.version_id, Chunk.ordinal, func.length(Chunk.text)).where(
                    Chunk.version_id.in_(version_ids)
                )
            )
        ).all()
        if version_ids
        else []
    )
    lengths = [int(r[2]) for r in sorted(rows, key=lambda r: (order[r[0]], r[1]))]
    settings = domain_settings()
    total = len(
        plan_summary_parts(
            lengths,
            single_pass=settings.summary_single_pass_chars,
            part_chars=settings.summary_batch_chars,
        )
    )
    parts = summary_parts(lengths, settings)
    return summary_price(parts, settings), parts, max(total, 1)


class SummaryService:
    def __init__(self, session: AsyncSession, settings: DomainSettings) -> None:
        self._session = session
        self._settings = settings

    async def _current_versions(self, user_id: uuid.UUID, topic_id: uuid.UUID) -> list[uuid.UUID]:
        try:
            return (await resolve_selection(self._session, user_id, topic_id, None)).version_ids
        except (NoSourcesSelectedError, NoReadySourcesError):
            return []

    async def _view(self, summary: Summary, **kwargs: bool) -> SummaryView:
        current = await self._current_versions(summary.user_id, summary.topic_id)
        return await summary_view(self._session, summary, current_version_ids=current, **kwargs)

    async def create(
        self,
        user_id: uuid.UUID,
        topic_id: uuid.UUID,
        *,
        source_ids: list[uuid.UUID] | None,
        idempotency_key: str | None,
    ) -> SummaryView:
        await get_topic(self._session, user_id, topic_id)
        if idempotency_key:
            existing = await self._session.scalar(
                select(Summary).where(
                    Summary.user_id == user_id, Summary.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.topic_id != topic_id:
                    raise SummaryNotFoundError("Конспект не найден.")
                return await self._view(existing, replay=True)
        selection = await resolve_selection(self._session, user_id, topic_id, source_ids)

        reusable = await self._reusable(topic_id, selection.version_ids)
        if reusable is not None:
            return await self._view(reusable, reused=True)

        price, _, _ = await summary_quote(self._session, selection.version_ids)
        reason = access.block_reason(
            await access.check(self._session, user_id, KIND_SUMMARY, price=price)
        )
        status = "blocked" if reason else "queued"
        stmt = (
            insert(Summary)
            .values(
                topic_id=topic_id,
                user_id=user_id,
                status=status,
                block_reason=reason,
                requested_source_ids=selection.source_ids,
                source_version_ids=selection.version_ids,
                content={"skippedSources": [s.model_dump(mode="json") for s in selection.skipped]},
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "idempotency_key"],
                index_where=Summary.idempotency_key.isnot(None),
            )
            .returning(Summary.id)
        )
        summary_id = await self._session.scalar(stmt)
        if summary_id is None:
            existing = await self._session.scalar(
                select(Summary).where(
                    Summary.user_id == user_id, Summary.idempotency_key == idempotency_key
                )
            )
            assert existing is not None
            return await self._view(existing, replay=True)
        if status == "queued":
            await jobs.enqueue(self._session, kind="summary", ref_id=summary_id, user_id=user_id)
        await touch_topic(self._session, topic_id)
        return await self._view(await get_summary(self._session, user_id, summary_id))

    async def _reusable(self, topic_id: uuid.UUID, version_ids: list[uuid.UUID]) -> Summary | None:
        """A succeeded summary over EXACTLY these versions — returned instead of a new charge."""
        wanted = sorted(version_ids)
        previous = (
            await self._session.scalars(
                select(Summary)
                .where(Summary.topic_id == topic_id, Summary.status == "succeeded")
                .order_by(Summary.created_at.desc())
                .limit(20)
            )
        ).all()
        return next((x for x in previous if sorted(x.source_version_ids) == wanted), None)

    async def price(
        self, user_id: uuid.UUID, topic_id: uuid.UUID, source_ids: list[uuid.UUID] | None
    ) -> SummaryPriceView:
        await get_topic(self._session, user_id, topic_id)
        selection = await resolve_selection(self._session, user_id, topic_id, source_ids)
        reusable = await self._reusable(topic_id, selection.version_ids)
        if reusable is not None:
            return SummaryPriceView(credits=0, parts=0, partial=False, reusedSummaryId=reusable.id)
        credits, parts, total = await summary_quote(self._session, selection.version_ids)
        return SummaryPriceView(credits=credits, parts=parts, partial=total > parts)

    async def latest(self, user_id: uuid.UUID, topic_id: uuid.UUID) -> SummaryView:
        await get_topic(self._session, user_id, topic_id)
        summary = await self._session.scalar(
            select(Summary)
            .where(Summary.topic_id == topic_id, Summary.user_id == user_id)
            .order_by(Summary.created_at.desc())
            .limit(1)
        )
        if summary is None:
            raise SummaryNotFoundError("Конспекта ещё нет.")
        return await self._view(summary)

    async def get(self, user_id: uuid.UUID, summary_id: uuid.UUID) -> SummaryView:
        return await self._view(await get_summary(self._session, user_id, summary_id))

    async def cancel(self, user_id: uuid.UUID, summary_id: uuid.UUID) -> SummaryView:
        summary = await get_summary(self._session, user_id, summary_id)
        if summary.status not in ("queued", "running"):
            raise InvalidStateError("Конспект уже готов или не строится.")
        summary.status = "canceled"
        summary.completed_at = now()
        summary.updated_at = now()
        await jobs.cancel_queued(self._session, kind="summary", ref_id=summary.id)
        await self._session.flush()
        return await self._view(summary)

    async def retry(self, user_id: uuid.UUID, summary_id: uuid.UUID) -> SummaryView:
        summary = await get_summary(self._session, user_id, summary_id)
        if summary.status not in ("failed", "blocked", "canceled"):
            raise InvalidStateError("Повторить можно только неудавшийся или отменённый конспект.")
        selection = await resolve_selection(
            self._session, user_id, summary.topic_id, list(summary.requested_source_ids)
        )
        price, _, _ = await summary_quote(self._session, selection.version_ids)
        reason = access.block_reason(
            await access.check(self._session, user_id, KIND_SUMMARY, price=price)
        )
        summary.status = "blocked" if reason else "queued"
        summary.block_reason = reason
        summary.error_code = None
        summary.error_message = None
        summary.completed_at = None
        summary.attempt = summary.attempt + 1
        summary.source_version_ids = selection.version_ids
        summary.content = {"skippedSources": [s.model_dump(mode="json") for s in selection.skipped]}
        summary.usage = None
        summary.updated_at = now()
        await self._session.flush()
        if summary.status == "queued":
            await jobs.enqueue(self._session, kind="summary", ref_id=summary.id, user_id=user_id)
        return await self._view(summary)
