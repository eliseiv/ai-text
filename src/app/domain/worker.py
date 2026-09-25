"""The background worker: ``python -m app.domain.worker``.

Runs next to the API (same image, same DB, same file volume). Processing continues on the server
whatever the phone does — the client only reads statuses back.

Failure policy per job: transient ⇒ re-queued with backoff (``WORKER_MAX_ATTEMPTS``); permanent or
out of attempts ⇒ the entity gets ``failed`` with a user-facing reason and the job ``failed``.
A job left ``running`` by a killed worker is re-claimed when its lease expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import jobs
from app.domain.config import DomainSettings, domain_settings
from app.domain.ingest import RetryableIngestError, mark_failed, run_ingest
from app.domain.ingest_web import WebFetcher
from app.domain.llm import LLMClient, build_llm_client
from app.domain.models import ChatMessage, Summary
from app.domain.provider import SourcesProvider
from app.domain.purge import purge_source, purge_topic
from app.domain.runs import RetryLater, run_answer, run_summary
from app.domain.sources import get_storage
from app.domain.storage import LocalFileStorage
from app.observability.logging import configure_logging, log_event

logger = logging.getLogger("app.domain.worker")


def build_fetcher(settings: DomainSettings) -> WebFetcher:
    return WebFetcher(
        max_bytes=settings.web_max_bytes,
        timeout=settings.web_timeout_seconds,
        max_redirects=settings.web_max_redirects,
        user_agent=settings.web_user_agent,
    )


def build_provider(
    settings: DomainSettings, llm: LLMClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> SourcesProvider:
    async def cancel_check(op: str, ref: str) -> bool:
        table = "chat_messages" if op == "answer" else "summaries"
        async with sessionmaker() as session:
            status = await session.scalar(
                text(f"SELECT status FROM {table} WHERE id = :id"), {"id": ref}
            )
        return status is None or status == "canceled"

    return SourcesProvider(
        llm,
        support_check=settings.citation_support_check,
        summary_single_pass_chars=settings.summary_single_pass_chars,
        summary_batch_chars=settings.summary_batch_chars,
        summary_max_batches=settings.summary_max_batches,
        cancel_check=cancel_check,
    )


class Worker:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: DomainSettings,
        storage: LocalFileStorage,
        fetcher: WebFetcher,
        provider: SourcesProvider,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._storage = storage
        self._fetcher = fetcher
        self._provider = provider

    async def run_once(self) -> bool:
        """Claim and process ONE job. ``False`` when the queue is empty."""
        async with self._sessionmaker() as session:
            job = await jobs.claim(session, lease_seconds=self._settings.worker_lease_seconds)
        if job is None:
            return False
        await self._process(job)
        return True

    async def drain(self, max_jobs: int = 1000) -> int:
        """Process until the queue is empty (tests, one-off runs)."""
        done = 0
        while done < max_jobs and await self.run_once():
            done += 1
        return done

    async def _dispatch(self, session: AsyncSession, job: jobs.ClaimedJob, last: bool) -> None:
        s = self._settings
        if job.kind == "ingest":
            await run_ingest(
                session,
                job.ref_id,
                settings=s,
                storage=self._storage,
                fetcher=self._fetcher,
                is_last_attempt=last,
            )
        elif job.kind == "answer":
            await run_answer(
                session, job.ref_id, provider=self._provider, settings=s, is_last_attempt=last
            )
        elif job.kind == "summary":
            await run_summary(
                session, job.ref_id, provider=self._provider, settings=s, is_last_attempt=last
            )
        elif job.kind == "purge_topic":
            await purge_topic(session, job.ref_id, self._storage)
        elif job.kind == "purge_source":
            await purge_source(session, job.ref_id, self._storage)

    async def _process(self, job: jobs.ClaimedJob) -> None:
        last = job.attempts >= self._settings.worker_max_attempts
        try:
            async with self._sessionmaker() as session:
                await self._dispatch(session, job, last)
            async with self._sessionmaker() as session:
                await jobs.finish(session, job.id)
                await session.commit()
        except (RetryLater, RetryableIngestError) as exc:
            await self._retry_or_fail(job, str(exc) or type(exc).__name__, last)
        except Exception as exc:  # noqa: BLE001 - one bad job must never stop the worker
            logger.exception(
                "job_crashed", extra={"extra_fields": {"jobId": str(job.id), "kind": job.kind}}
            )
            await self._retry_or_fail(job, type(exc).__name__, last)

    async def _retry_or_fail(self, job: jobs.ClaimedJob, error: str, last: bool) -> None:
        async with self._sessionmaker() as session:
            if not last:
                await jobs.retry_later(
                    session, job.id, delay_seconds=jobs.backoff_seconds(job.attempts), error=error
                )
                await session.commit()
                return
            await jobs.fail(session, job.id, error=error)
            await session.commit()
            await self._fail_entity(session, job.kind, job.ref_id)
        log_event(
            logger, logging.WARNING, "job_failed", jobId=str(job.id), kind=job.kind, error=error
        )

    async def _fail_entity(self, session: AsyncSession, kind: str, ref_id: uuid.UUID) -> None:
        message = "Не удалось обработать запрос. Попробуйте ещё раз — средства не списаны."
        if kind == "ingest":
            await mark_failed(
                session,
                ref_id,
                "internal_error",
                "Не удалось обработать материал. Попробуйте ещё раз.",
            )
            return
        model: Any = {"answer": ChatMessage, "summary": Summary}.get(kind)
        if model is None:
            return
        await session.execute(
            update(model)
            .where(model.id == ref_id, model.status.in_(("queued", "running")))
            .values(status="failed", error_code="internal_error", error_message=message)
        )
        await session.commit()

    async def run_forever(self, stop: asyncio.Event) -> None:
        async def loop(n: int) -> None:
            while not stop.is_set():
                try:
                    worked = await self.run_once()
                except Exception:  # noqa: BLE001 - DB blip: back off, keep the loop alive
                    logger.exception("worker_loop_error")
                    worked = False
                if not worked:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop.wait(), timeout=self._settings.worker_poll_seconds
                        )

        await asyncio.gather(*(loop(i) for i in range(max(self._settings.worker_concurrency, 1))))


def build_worker(
    llm_factory: Callable[[DomainSettings], LLMClient] = build_llm_client,
) -> Worker:
    from app.db import get_sessionmaker

    settings = domain_settings()
    sessionmaker = get_sessionmaker()
    return Worker(
        sessionmaker=sessionmaker,
        settings=settings,
        storage=get_storage(),
        fetcher=build_fetcher(settings),
        provider=build_provider(settings, llm_factory(settings), sessionmaker),
    )


async def _main() -> None:
    settings = domain_settings()
    configure_logging(
        settings.log_level,
        service=f"{settings.service_name}-worker",
        version=settings.service_version,
    )
    worker = build_worker()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows dev has no signal handlers
            loop.add_signal_handler(sig, stop.set)
    log_event(logger, logging.INFO, "worker_started", concurrency=settings.worker_concurrency)
    await worker.run_forever(stop)
    from app.db import dispose_engine

    await dispose_engine()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
