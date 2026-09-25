"""Assembling what the model sees — the isolation boundary of the AI.

The context is built ONLY from:
* chunks of the source versions recorded on the request, re-filtered at run time to sources that
  are still alive, ready and selected in the SAME topic of the SAME user (deleted/excluded
  between «send» and «run» ⇒ not used);
* for answers, earlier Q&A of the same topic whose sources are all still allowed — an old AI
  answer built on a now-deleted source is not fed back into new answers.

Nothing a document says can widen this set: the queries below have no input from the model.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ChatMessage, Chunk, Source, SourceVersion

_WORD = re.compile(r"\w{3,}", re.UNICODE)


class NoUsableSources(Exception):
    pass


@dataclass
class BuiltContext:
    chunks: list[dict[str, Any]]
    versions: dict[str, dict[str, Any]]
    version_ids: list[uuid.UUID]
    partial: bool
    total_chars: int


async def allowed_versions(
    session: AsyncSession,
    user_id: uuid.UUID,
    topic_id: uuid.UUID,
    version_ids: Sequence[uuid.UUID],
) -> list[tuple[SourceVersion, Source]]:
    if not version_ids:
        return []
    rows = (
        await session.execute(
            select(SourceVersion, Source)
            .join(Source, Source.id == SourceVersion.source_id)
            .where(
                SourceVersion.id.in_(list(version_ids)),
                Source.user_id == user_id,
                Source.topic_id == topic_id,
                Source.deleted_at.is_(None),
                Source.selected.is_(True),
                Source.status == "ready",
            )
            .order_by(Source.created_at)
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


def _versions_map(pairs: Sequence[tuple[SourceVersion, Source]]) -> dict[str, dict[str, Any]]:
    return {
        str(v.id): {
            "source_id": str(s.id),
            "title": s.title,
            "kind": s.kind,
            "pages": v.pages,
            "char_count": v.char_count,
        }
        for v, s in pairs
    }


def _chunk_dict(key: str, chunk: Any) -> dict[str, Any]:
    return {
        "key": key,
        "chunk_id": str(chunk.id),
        "version_id": str(chunk.version_id),
        "ordinal": int(chunk.ordinal),
        "start": int(chunk.start),
        "end": int(chunk.end),
        "text": chunk.text,
    }


async def _all_chunks(session: AsyncSession, version_ids: list[uuid.UUID]) -> list[Any]:
    rows = (
        await session.scalars(
            select(Chunk).where(Chunk.version_id.in_(version_ids)).order_by(Chunk.ordinal)
        )
    ).all()
    order = {v: i for i, v in enumerate(version_ids)}
    return sorted(rows, key=lambda c: (order[c.version_id], c.ordinal))


async def build_context(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    topic_id: uuid.UUID,
    version_ids: Sequence[uuid.UUID],
    budget_chars: int | None,
    query: str | None = None,
) -> BuiltContext:
    """All chunks while they fit ``budget_chars`` (``None`` = no limit); above it, full-text
    retrieval by ``query`` picks the most relevant chunks and the context is marked partial."""
    pairs = await allowed_versions(session, user_id, topic_id, version_ids)
    if not pairs:
        raise NoUsableSources()
    ordered_ids = [v.id for v, _ in pairs]
    total = sum(v.char_count for v, _ in pairs)
    partial = False
    if budget_chars is None or total <= budget_chars:
        chosen = await _all_chunks(session, ordered_ids)
    else:
        partial = True
        chosen = await _ranked_chunks(session, ordered_ids, query or "", budget_chars)
    chunks = [_chunk_dict(f"c{i + 1}", c) for i, c in enumerate(chosen)]
    if not chunks:
        raise NoUsableSources()
    return BuiltContext(
        chunks=chunks,
        versions=_versions_map(pairs),
        version_ids=ordered_ids,
        partial=partial,
        total_chars=total,
    )


async def _ranked_chunks(
    session: AsyncSession, version_ids: list[uuid.UUID], query: str, budget: int
) -> list[Any]:
    words = list(dict.fromkeys(w.lower() for w in _WORD.findall(query)))[:24]
    ids = [str(v) for v in version_ids]
    if words:
        # Only \w tokens joined by '|' reach to_tsquery — no operator injection possible.
        q = " | ".join(words)
        rows = (
            await session.execute(
                text(
                    "SELECT id FROM chunks, "
                    "  (SELECT to_tsquery('russian', :q) || to_tsquery('english', :q) AS query) t "
                    "WHERE version_id = ANY(CAST(:ids AS uuid[])) "
                    "ORDER BY ts_rank_cd(tsv, t.query) DESC, ordinal LIMIT 2000"
                ),
                {"q": q, "ids": ids},
            )
        ).all()
        ranked_ids = [r[0] for r in rows]
    else:
        ranked_ids = []
    all_chunks = {c.id: c for c in await _all_chunks(session, version_ids)}
    if not ranked_ids:
        ranked_ids = list(all_chunks)
    chosen: list[Any] = []
    used = 0
    for cid in ranked_ids:
        chunk = all_chunks.get(cid)
        if chunk is None:
            continue
        if used + len(chunk.text) > budget:
            continue
        chosen.append(chunk)
        used += len(chunk.text)
    order = {v: i for i, v in enumerate(version_ids)}
    return sorted(chosen, key=lambda c: (order[c.version_id], c.ordinal))


async def answer_history(
    session: AsyncSession,
    *,
    topic_id: uuid.UUID,
    before_seq: int,
    allowed_version_ids: Sequence[uuid.UUID],
    limit: int,
) -> list[tuple[str, str]]:
    """Previous answered turns, oldest first, excluding any built on a no-longer-allowed source."""
    if limit <= 0:
        return []
    allowed = set(allowed_version_ids)
    rows = (
        await session.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.topic_id == topic_id,
                ChatMessage.seq < before_seq,
                ChatMessage.status == "succeeded",
            )
            .order_by(ChatMessage.seq.desc())
            .limit(limit * 3)
        )
    ).all()
    history: list[tuple[str, str]] = []
    for message in rows:
        answer = message.answer or {}
        if answer.get("status") != "answered":
            continue
        if not set(message.source_version_ids) <= allowed:
            continue
        history.append((message.question, str(answer.get("text", ""))[:2000]))
        if len(history) >= limit:
            break
    return list(reversed(history))
