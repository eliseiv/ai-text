"""Generation schemas.

Note the shape of the response: a business BLOCK is a ``200`` with ``status="blocked"``, not a 4xx.
It is a valid answer to a valid request ("you may not generate right now, here is why"), and the
client renders it as a screen, not as an error.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import Field

from app.schemas.common import StrictModel


class GenerateRequest(StrictModel):
    """The DOMAIN owns this shape — this is the template's sample (EchoProvider).

    Note what is missing: no ``credits``, no ``userId``. The price is computed server-side from
    the provider's usage; the user is the JWT ``sub``.
    """

    model: str | None = Field(default=None, max_length=128)
    params: dict[str, Any] = Field(
        default_factory=dict, description="Параметры генерации. Их форму определяет сервис."
    )


class GenerateResponse(StrictModel):
    status: str = Field(
        description="`succeeded` — результат готов; `blocked` — генерация недоступна (см. "
        "`blockReason`), она **не запускалась** и кредиты не списаны."
    )
    generationId: uuid.UUID | None = None
    blockReason: str | None = Field(
        default=None,
        description="`trial_used` | `subscription_required` | `subscription_expired` | "
        "`credits_empty` | `policy_denied`.",
    )
    output: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    creditsCharged: int = 0
    newBalance: int | None = None
    idempotentReplay: bool = False


class GenerationView(StrictModel):
    id: uuid.UUID
    kind: str
    model: str | None = None
    status: str
    creditsCharged: int
    units: int
    unitKind: str
    totalTokens: int
    latencyMs: int | None = None
    errorCode: str | None = None
    providerRef: str | None = None
    createdAt: datetime.datetime
    completedAt: datetime.datetime | None = None


class GenerationDetail(GenerationView):
    output: dict[str, Any] | None = Field(
        default=None, description="Результат генерации (если он не превысил порог хранения)."
    )


class GenerationsListResponse(StrictModel):
    items: list[GenerationView]
    nextCursor: str | None = None


class GenerationStatsItem(StrictModel):
    kind: str
    generations: int
    succeeded: int
    failed: int
    units: int
    totalTokens: int
    creditsCharged: int
    lastGenerationAt: datetime.datetime | None = None


class GenerationStatsResponse(StrictModel):
    totals: list[GenerationStatsItem]
