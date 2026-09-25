"""Profile schemas. No "whose profile" parameter exists anywhere."""

from __future__ import annotations

import datetime
import uuid

from pydantic import Field

from app.schemas.common import StrictModel


class ProfileResponse(StrictModel):
    userId: uuid.UUID
    accountId: str = Field(
        description="Человекочитаемый идентификатор для саппорта (`8472-1936-AXQ5`). Производный, "
        "не хранится."
    )
    displayName: str | None = None
    createdAt: datetime.datetime | None = None


class ProfileUpdateRequest(StrictModel):
    displayName: str | None = Field(
        default=None, max_length=100, description="`null` — сбросить имя."
    )
