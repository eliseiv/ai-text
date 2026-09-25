"""Shared dependencies of the domain routers."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, Request

from app.api_gateway import rate_limit
from app.deps import CurrentUser, DbSession, client_ip
from app.domain.chat import ChatService
from app.domain.config import DomainSettings, domain_settings
from app.domain.sources import SourceService, get_storage
from app.domain.summaries import SummaryService
from app.domain.topics import TopicService
from app.errors import RateLimitedError, ValidationFailedError


async def limit_other(current: CurrentUser) -> uuid.UUID:
    # Called through the module so the test-suite's limiter patch applies here too.
    if not await rate_limit.enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    return current.user_id


async def limit_generation(current: CurrentUser, request: Request) -> uuid.UUID:
    if not await rate_limit.enforce_generation_limits(
        user_id=current.user_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")
    return current.user_id


UserId = Annotated[uuid.UUID, Depends(limit_other)]
GenerationUserId = Annotated[uuid.UUID, Depends(limit_generation)]


def idempotency_key(
    key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Повтор запроса с тем же ключом (например, после потери сети) вернёт тот "
            "же объект и не создаст дубликат и повторное списание.",
        ),
    ] = None,
) -> str | None:
    if key is None:
        return None
    key = key.strip()
    if not key or len(key) > 200:
        raise ValidationFailedError("Idempotency-Key must be 1..200 characters")
    return key


IdempotencyKey = Annotated[str | None, Depends(idempotency_key)]


def get_domain_settings() -> DomainSettings:
    return domain_settings()


Settings = Annotated[DomainSettings, Depends(get_domain_settings)]


def topic_service(session: DbSession, settings: Settings) -> TopicService:
    return TopicService(session, settings)


def source_service(session: DbSession, settings: Settings) -> SourceService:
    return SourceService(session, settings, get_storage())


def chat_service(session: DbSession, settings: Settings) -> ChatService:
    return ChatService(session, settings)


def summary_service(session: DbSession, settings: Settings) -> SummaryService:
    return SummaryService(session, settings)


Topics = Annotated[TopicService, Depends(topic_service)]
Sources = Annotated[SourceService, Depends(source_service)]
Chat = Annotated[ChatService, Depends(chat_service)]
Summaries = Annotated[SummaryService, Depends(summary_service)]


def public_base_url(request: Request, settings: DomainSettings) -> str:
    domain = settings.normalized_service_domain()
    return f"https://{domain}" if domain else str(request.base_url).rstrip("/")
