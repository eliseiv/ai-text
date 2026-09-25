"""Physical deletion after the agreed delay: files, versions, chunks (index), history, summaries.

Topic purge deletes the topic row — ``ON DELETE CASCADE`` removes sources, versions, chunks,
chat messages and summaries in one statement; files are removed from storage first. After this,
old API links and old file links answer 404.

Source purge keeps a tombstone ``sources`` row (``deleted_at`` set, text/files gone) so an old
answer can still say «Источник удалён» for its citations instead of pointing at nothing.

Billing records (``generations``, ``ledger_transactions``, ``payments``) are NOT touched: they are
the accounting trail of the user's money and contain no document content.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Source, SourceVersion, Topic
from app.domain.storage import LocalFileStorage


async def purge_topic(
    session: AsyncSession, topic_id: uuid.UUID, storage: LocalFileStorage
) -> None:
    topic = await session.scalar(select(Topic).where(Topic.id == topic_id))
    if topic is None:
        return
    if topic.deleted_at is None:
        return  # restored or never deleted — nothing to purge
    sources = (
        await session.execute(select(Source.id, Source.user_id).where(Source.topic_id == topic_id))
    ).all()
    for source_id, user_id in sources:
        await asyncio.to_thread(storage.delete_source, user_id, source_id)
    await session.execute(delete(Topic).where(Topic.id == topic_id))
    await session.commit()


async def purge_source(
    session: AsyncSession, source_id: uuid.UUID, storage: LocalFileStorage
) -> None:
    source = await session.scalar(select(Source).where(Source.id == source_id))
    if source is None or source.deleted_at is None:
        return
    await asyncio.to_thread(storage.delete_source, source.user_id, source.id)
    # Versions cascade to chunks: text and index are gone; the tombstone row stays.
    await session.execute(delete(SourceVersion).where(SourceVersion.source_id == source_id))
    await session.execute(
        update(Source)
        .where(Source.id == source_id)
        .values(file_key=None, file_size=0, current_version_id=None, content_sha256=None)
    )
    await session.commit()
