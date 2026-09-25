"""Worker handlers for answers and summaries — the bridge to the core ``GenerationService``.

The core ``run()`` owns the money path: policy re-check, idempotency anchor
(``answer:{message_id}:{attempt}``), provider call, charge-only-on-success, ledger, audit. A job
re-run after a crash hits the same key: finished ⇒ idempotent replay (0 credits, same result);
still ``running`` past the lease ⇒ the orphaned anchor is failed and the run is repeated — never a
second charge.

Demo questions (``is_demo_free``) call the provider directly: they are free by product decision
and bounded by ``DEMO_FREE_QUESTIONS``.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import AuditService
from app.domain.config import DomainSettings
from app.domain.context import NoUsableSources, answer_history, build_context
from app.domain.models import ChatMessage, Summary, Topic
from app.domain.pricing import KIND_ANSWER, KIND_SUMMARY
from app.domain.provider import SourcesProvider
from app.errors import AlreadyInProgressError, TooManyInflightError, UpstreamError
from app.generation.contract import GenerationRequest, ProviderError
from app.generation.registry import get_pricing
from app.generation.repository import GenerationsRepository
from app.generation.service import GenerationOutcome, GenerationService
from app.observability.logging import log_event
from app.wallet.service import WalletService

logger = logging.getLogger("app.domain.runs")

_MESSAGES = {
    "no_sources": "Выбранные материалы удалены или исключены. Выберите материалы и повторите.",
    "llm_refusal": "Модель отказалась обрабатывать этот запрос.",
    "timeout": "Ответ готовился слишком долго. Попробуйте ещё раз.",
    "canceled": "Запрос отменён.",
    "result_lost": "Не удалось восстановить результат. Повторите запрос.",
}
_DEFAULT_ERROR = "Не удалось получить ответ. Попробуйте ещё раз — средства не списаны."
_RETRYABLE = {"llm_unavailable", "llm_rate_limited", "timeout", "llm_http_500", "llm_http_529"}


class RetryLater(Exception):
    """Transient condition (too many inflight, upstream hiccup): re-queue the job."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _service(
    session: AsyncSession, provider: SourcesProvider, settings: DomainSettings
) -> GenerationService:
    return GenerationService(
        session=session,
        repo=GenerationsRepository(session),
        wallet=WalletService(session, AuditService()),
        audit=AuditService(),
        provider=provider,
        pricing=get_pricing(),
        settings=settings,
    )


async def _run_billed(
    session: AsyncSession,
    service: GenerationService,
    *,
    user_id: uuid.UUID,
    kind: str,
    params: dict[str, Any],
    key: str,
    settings: DomainSettings,
) -> GenerationOutcome:
    try:
        return await service.run(user_id=user_id, kind=kind, params=params, idempotency_key=key)
    except AlreadyInProgressError as exc:
        repo = GenerationsRepository(session)
        row = await repo.get_by_key(user_id, key)
        stale_after = datetime.timedelta(seconds=settings.worker_lease_seconds)
        if (
            row is not None
            and row.status in ("pending", "running")
            and _now() - row.created_at > stale_after
        ):
            # The worker that anchored it died mid-call. Fail the orphan (it was never charged:
            # the debit happens after the provider returns) and run again on the same key.
            await repo.fail(row.id, error_code="worker_lost", error_message=None)
            await session.commit()
            return await service.run(user_id=user_id, kind=kind, params=params, idempotency_key=key)
        raise RetryLater("generation in progress") from exc
    except TooManyInflightError as exc:
        raise RetryLater("too many inflight") from exc


async def _error_code(session: AsyncSession, user_id: uuid.UUID, key: str) -> str:
    row = await GenerationsRepository(session).get_by_key(user_id, key)
    return (row.error_code if row else None) or "upstream_error"


async def _generate_free(
    provider: SourcesProvider,
    *,
    user_id: uuid.UUID,
    kind: str,
    params: dict[str, Any],
    key: str,
    settings: DomainSettings,
) -> dict[str, Any]:
    result = await provider.generate(
        GenerationRequest(
            user_id=user_id,
            kind=kind,
            idempotency_key=key,
            params=params,
            model=None,
            request_id="",
            deadline_s=settings.generation_timeout_seconds,
        )
    )
    return result.output


async def run_answer(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    provider: SourcesProvider,
    settings: DomainSettings,
    is_last_attempt: bool,
) -> None:
    message = await session.scalar(select(ChatMessage).where(ChatMessage.id == message_id))
    if message is None or message.status not in ("queued", "running"):
        return
    topic = await session.scalar(select(Topic).where(Topic.id == message.topic_id))
    if topic is None or topic.deleted_at is not None:
        return
    skipped = (message.answer or {}).get("skippedSources", [])
    message.status = "running"
    message.updated_at = _now()
    await session.commit()

    try:
        ctx = await build_context(
            session,
            user_id=message.user_id,
            topic_id=message.topic_id,
            version_ids=message.source_version_ids,
            budget_chars=settings.answer_context_chars,
            query=message.question,
        )
    except NoUsableSources:
        await _finish(session, message, status="failed", error_code="no_sources")
        return
    history = await answer_history(
        session,
        topic_id=message.topic_id,
        before_seq=message.seq,
        allowed_version_ids=ctx.version_ids,
        limit=settings.answer_history_turns,
    )
    message.source_version_ids = ctx.version_ids
    await session.commit()
    params: dict[str, Any] = {
        "op": "answer",
        "ref": str(message.id),
        "question": message.question,
        "history": history,
        "chunks": ctx.chunks,
        "versions": ctx.versions,
        "partialContext": ctx.partial,
    }
    key = f"answer:{message.id}:{message.attempt}"
    await _execute(
        session,
        message,
        provider=provider,
        settings=settings,
        kind=KIND_ANSWER,
        params=params,
        key=key,
        free=message.is_demo_free,
        extra={"skippedSources": skipped},
        is_last_attempt=is_last_attempt,
    )


async def run_summary(
    session: AsyncSession,
    summary_id: uuid.UUID,
    *,
    provider: SourcesProvider,
    settings: DomainSettings,
    is_last_attempt: bool,
) -> None:
    summary = await session.scalar(select(Summary).where(Summary.id == summary_id))
    if summary is None or summary.status not in ("queued", "running"):
        return
    topic = await session.scalar(select(Topic).where(Topic.id == summary.topic_id))
    if topic is None or topic.deleted_at is not None:
        return
    skipped = (summary.content or {}).get("skippedSources", [])
    summary.status = "running"
    summary.updated_at = _now()
    await session.commit()
    try:
        # A summary reads the WHOLE text (no retrieval); the provider map-reduces and reports
        # coverage if it cannot read everything.
        ctx = await build_context(
            session,
            user_id=summary.user_id,
            topic_id=summary.topic_id,
            version_ids=summary.source_version_ids,
            budget_chars=None,
        )
    except NoUsableSources:
        await _finish(session, summary, status="failed", error_code="no_sources")
        return
    summary.source_version_ids = ctx.version_ids
    await session.commit()
    params = {
        "op": "summary",
        "ref": str(summary.id),
        "chunks": ctx.chunks,
        "versions": ctx.versions,
    }
    await _execute(
        session,
        summary,
        provider=provider,
        settings=settings,
        kind=KIND_SUMMARY,
        params=params,
        key=f"summary:{summary.id}:{summary.attempt}",
        free=False,
        extra={"skippedSources": skipped},
        is_last_attempt=is_last_attempt,
    )


async def _execute(
    session: AsyncSession,
    entity: ChatMessage | Summary,
    *,
    provider: SourcesProvider,
    settings: DomainSettings,
    kind: str,
    params: dict[str, Any],
    key: str,
    free: bool,
    extra: dict[str, Any],
    is_last_attempt: bool,
) -> None:
    user_id = entity.user_id
    if free:
        try:
            output = await _generate_free(
                provider, user_id=user_id, kind=kind, params=params, key=key, settings=settings
            )
        except ProviderError as exc:
            await _on_failure(session, entity, exc.code, exc.retryable, is_last_attempt)
            return
        await _finish(
            session,
            entity,
            status="succeeded",
            output={**output, **extra},
            usage={"creditsCharged": 0, "free": True},
        )
        return

    try:
        outcome = await _run_billed(
            session,
            _service(session, provider, settings),
            user_id=user_id,
            kind=kind,
            params=params,
            key=key,
            settings=settings,
        )
    except UpstreamError:
        code = await _error_code(session, user_id, key)
        retryable = code in _RETRYABLE or code.startswith("llm_http_5")
        await _on_failure(session, entity, code, retryable, is_last_attempt)
        return

    if outcome.status == "blocked":
        await _finish(session, entity, status="blocked", block_reason=outcome.block_reason)
        return
    if outcome.output is None:  # replay of a result too large for the core's meta column
        await _finish(session, entity, status="failed", error_code="result_lost")
        return
    await _finish(
        session,
        entity,
        status="succeeded",
        output={**outcome.output, **extra},
        usage={"creditsCharged": outcome.credits_charged, "free": outcome.credits_charged == 0},
        generation_id=outcome.generation_id,
    )


async def _on_failure(
    session: AsyncSession,
    entity: ChatMessage | Summary,
    code: str,
    retryable: bool,
    is_last_attempt: bool,
) -> None:
    await session.refresh(entity)
    if entity.status == "canceled" or code == "canceled":
        await _finish(session, entity, status="canceled")
        return
    if retryable and not is_last_attempt:
        raise RetryLater(code)
    await _finish(session, entity, status="failed", error_code=code)


async def _finish(
    session: AsyncSession,
    entity: ChatMessage | Summary,
    *,
    status: str,
    output: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error_code: str | None = None,
    block_reason: str | None = None,
    generation_id: uuid.UUID | None = None,
) -> None:
    await session.refresh(entity)
    if entity.status == "canceled" and status != "succeeded":
        return  # the user already stopped waiting; keep «canceled»
    entity.status = status
    entity.block_reason = block_reason
    entity.error_code = error_code
    entity.error_message = _MESSAGES.get(error_code, _DEFAULT_ERROR) if error_code else None
    if output is not None:
        if isinstance(entity, ChatMessage):
            entity.answer = output
        else:
            entity.content = output
    if usage is not None:
        entity.usage = usage
    if generation_id is not None:
        entity.generation_id = generation_id
    entity.completed_at = (
        _now() if status in ("succeeded", "failed", "canceled", "blocked") else None
    )
    entity.updated_at = _now()
    await session.commit()
    log_event(
        logger,
        logging.INFO,
        "run_finished",
        entity=type(entity).__name__,
        entityId=str(entity.id),
        status=status,
        errorCode=error_code,
        blockReason=block_reason,
    )
