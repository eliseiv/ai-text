"""Admin schemas. Body ≤ 8 KB, ``extra='forbid'``."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import Field, model_validator

from app.schemas.common import StrictModel


class AdminGrantRequest(StrictModel):
    userId: uuid.UUID
    credits: int = Field(gt=0, description="Сколько кредитов начислить.")
    idempotencyKey: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Обязателен: двойной клик оператора не должен начислить дважды. Делайте его "
            "осмысленным (`support-ticket-1234`) — он попадает в audit и даёт атрибуцию."
        ),
    )
    reason: str | None = Field(default=None, max_length=500)


class AdminGrantResponse(StrictModel):
    creditsGranted: int
    newBalance: int
    ledgerTxId: uuid.UUID
    idempotentReplay: bool


class AdminSubscriptionGrantRequest(StrictModel):
    userId: uuid.UUID
    expiresAt: datetime.datetime | None = Field(
        default=None, description="Ровно одно из `expiresAt` / `days`. Строго в будущем."
    )
    days: int | None = Field(default=None, gt=0, description="Ровно одно из `expiresAt` / `days`.")
    plan: str = Field(default="manual_grant", max_length=128)
    credits: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Опущено → начислить `SUBSCRIPTION_CREDITS_PER_PERIOD` (дефолт обязан давать "
            "**работающий** доступ). `0` → активировать без начисления. `N` → начислить ровно N."
        ),
    )
    idempotencyKey: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _exactly_one_term(self) -> AdminSubscriptionGrantRequest:
        if (self.expiresAt is None) == (self.days is None):
            raise ValueError("exactly one of expiresAt / days is required")
        if self.expiresAt is not None:
            if self.expiresAt.tzinfo is None:
                raise ValueError("expiresAt must be timezone-aware")
            # Lazy expiry would treat a past expiresAt as `expired` on the very first read — the
            # grant would be useless. Validate loudly instead of trusting the operator's attention.
            if self.expiresAt <= datetime.datetime.now(tz=datetime.UTC):
                raise ValueError("expiresAt must be in the future")
        return self

    def resolved_expires_at(self) -> datetime.datetime:
        if self.expiresAt is not None:
            return self.expiresAt
        if self.days is None:  # pragma: no cover - the validator guarantees exactly one of them
            # Not an assert: `python -O` strips asserts, and this is a money/access path.
            raise ValueError("exactly one of expiresAt / days is required")
        return datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=self.days)


class AdminSubscriptionGrantResponse(StrictModel):
    status: str
    expiresAt: datetime.datetime
    plan: str
    creditsGranted: int
    newBalance: int | None = None
    ledgerTxId: uuid.UUID | None = None
    idempotentReplay: bool | None = None


class AdminWalletResponse(StrictModel):
    userId: uuid.UUID
    balance: int
    updatedAt: datetime.datetime | None = None
    recentTransactions: list[dict[str, Any]]
