"""Durable job queue in PostgreSQL.

Why not Redis/Celery: the jobs must survive restarts and be enqueued in the SAME transaction as the
row they process (a source row committed without its ingest job — or the reverse — is exactly the
«статус завис навсегда» bug). One table, three statements:

* ``enqueue`` — ``INSERT … ON CONFLICT DO NOTHING`` on the partial unique index: at most one active
  job per ``(kind, ref_id)``, so a network retry or a double tap cannot double-process;
* ``claim`` — ``FOR UPDATE SKIP LOCKED``: N workers never take the same job; a job whose lease
  expired (worker killed mid-run) is taken again;
* ``finish`` / ``retry_later`` / ``fail``.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    kind: str
    ref_id: uuid.UUID
    user_id: uuid.UUID | None
    attempts: int


async def enqueue(
    session: AsyncSession,
    *,
    kind: str,
    ref_id: uuid.UUID,
    user_id: uuid.UUID | None,
    delay_seconds: float = 0,
) -> bool:
    """Queue a job in the CALLER's transaction. ``False`` = an active job already exists."""
    inserted = await session.scalar(
        text(
            "INSERT INTO domain_jobs (kind, ref_id, user_id, run_after) "
            "VALUES (:kind, :ref, :uid, now() + make_interval(secs => :delay)) "
            "ON CONFLICT (kind, ref_id) WHERE status IN ('queued', 'running') DO NOTHING "
            "RETURNING id"
        ),
        {
            "kind": kind,
            "ref": str(ref_id),
            "uid": str(user_id) if user_id else None,
            "delay": float(delay_seconds),
        },
    )
    return inserted is not None


async def claim(session: AsyncSession, *, lease_seconds: int) -> ClaimedJob | None:
    row = (
        await session.execute(
            text(
                "UPDATE domain_jobs SET status = 'running', attempts = attempts + 1, "
                "locked_until = now() + make_interval(secs => :lease), updated_at = now() "
                "WHERE id = ("
                "  SELECT id FROM domain_jobs "
                "  WHERE (status = 'queued' AND run_after <= now()) "
                "     OR (status = 'running' AND locked_until < now()) "
                "  ORDER BY run_after LIMIT 1 FOR UPDATE SKIP LOCKED"
                ") RETURNING id, kind, ref_id, user_id, attempts"
            ),
            {"lease": float(lease_seconds)},
        )
    ).first()
    await session.commit()
    if row is None:
        return None
    return ClaimedJob(id=row[0], kind=row[1], ref_id=row[2], user_id=row[3], attempts=int(row[4]))


async def finish(session: AsyncSession, job_id: uuid.UUID) -> None:
    await session.execute(
        text(
            "UPDATE domain_jobs SET status = 'done', locked_until = NULL, updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": str(job_id)},
    )


async def retry_later(
    session: AsyncSession, job_id: uuid.UUID, *, delay_seconds: float, error: str
) -> None:
    await session.execute(
        text(
            "UPDATE domain_jobs SET status = 'queued', locked_until = NULL, last_error = :err, "
            "run_after = now() + make_interval(secs => :delay), updated_at = now() WHERE id = :id"
        ),
        {"id": str(job_id), "delay": float(delay_seconds), "err": error[:500]},
    )


async def fail(session: AsyncSession, job_id: uuid.UUID, *, error: str) -> None:
    await session.execute(
        text(
            "UPDATE domain_jobs SET status = 'failed', locked_until = NULL, last_error = :err, "
            "updated_at = now() WHERE id = :id"
        ),
        {"id": str(job_id), "err": error[:500]},
    )


async def cancel_queued(session: AsyncSession, *, kind: str, ref_id: uuid.UUID) -> None:
    """Drop a not-yet-started job (the entity was canceled/deleted)."""
    await session.execute(
        text(
            "UPDATE domain_jobs SET status = 'done', last_error = 'canceled', updated_at = now() "
            "WHERE kind = :kind AND ref_id = :ref AND status = 'queued'"
        ),
        {"kind": kind, "ref": str(ref_id)},
    )


def backoff_seconds(attempts: int) -> float:
    return float(min(30 * 2 ** max(attempts - 1, 0), 900))


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
