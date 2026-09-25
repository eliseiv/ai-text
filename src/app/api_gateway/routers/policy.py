"""Policy route: GET /v1/policy/effective."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, DbSession
from app.errors import RateLimitedError
from app.policy.loader import effective
from app.schemas.policy import EffectivePolicyResponse

router = APIRouter(prefix="/v1/policy", tags=["Policy"])


@router.get(
    "/effective",
    response_model=EffectivePolicyResponse,
    summary="Эффективные права пользователя",
    description=(
        "Отвечает на вопрос «можно ли сейчас генерировать и почему нет» **до** попытки "
        "генерации. Использует ту же функцию решения, что и сама генерация, — расхождение «UI "
        "показал можно, генерация вернула blocked» невозможно by construction.\n\n"
        "`requiredCredits` — оценка стоимости планируемой генерации (дефолт `1`). UI, знающий "
        "цену (например, выбрана более дорогая модель), передаёт её и заранее получает "
        "корректный `credits_empty`.\n\n"
        "Права возвращаются только для `sub` из токена: параметра «чей policy показать» нет."
    ),
)
async def policy_effective(
    current: CurrentUser,
    session: DbSession,
    requiredCredits: Annotated[  # noqa: N803 - camelCase query param per the API contract
        int, Query(ge=1, description="Стоимость планируемой генерации в кредитах.")
    ] = 1,
) -> EffectivePolicyResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    result = await effective(session, current.user_id, required_credits=requiredCredits)
    return EffectivePolicyResponse(
        allowed=result.allowed,
        reasons=[r.value for r in result.reasons],
        subscriptionStatus=result.subscription_status.value,
        creditsBalance=result.credits_balance,
        trialUsed=result.trial_used,
        requiredCredits=result.required_credits,
        billingKind=result.billing_kind.value,
    )
