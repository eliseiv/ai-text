"""Schemas of ``GET /v1/policy/effective``."""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import StrictModel


class EffectivePolicyResponse(StrictModel):
    allowed: bool = Field(description="Можно ли запускать генерацию **сейчас**.")
    reasons: list[str] = Field(
        default_factory=list,
        description=(
            "Причины блокировки — те же значения, что и `blockReason` у генерации: "
            "`trial_used` | `subscription_required` | `subscription_expired` | `credits_empty` | "
            "`policy_denied`. `rate_limited` здесь не приходит — это HTTP `429`."
        ),
    )
    subscriptionStatus: str = Field(
        description=(
            "`active` | `expired` | `none` — **после** ленивого истечения, т.е. ровно то, что "
            "увидит генерация."
        )
    )
    creditsBalance: int = Field(description="Текущий баланс кредитов.")
    trialUsed: bool = Field(description="Израсходована ли пожизненная бесплатная генерация.")
    requiredCredits: int = Field(
        description="Стоимость, для которой посчитан ответ (query-параметр `requiredCredits`)."
    )
    billingKind: str = Field(
        description=(
            "Как будет оплачена следующая генерация: `credits` | `trial` | `none` "
            "(`none` — генерация недоступна)."
        )
    )
