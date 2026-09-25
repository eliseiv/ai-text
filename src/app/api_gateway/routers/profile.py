"""Profile routes. There is no "whose profile" parameter — by construction."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_profile_service
from app.errors import RateLimitedError
from app.profile.service import ProfileService
from app.schemas.profile import ProfileResponse, ProfileUpdateRequest

router = APIRouter(prefix="/v1/profile", tags=["Profile"])


@router.get(
    "",
    response_model=ProfileResponse,
    summary="Профиль пользователя",
    description=(
        "Имя и человекочитаемый `accountId` (его диктуют саппорту). Профиль может ещё не "
        "существовать — тогда возвращаются значения по умолчанию, а не `404`."
    ),
)
async def get_profile(
    current: CurrentUser,
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    view = await service.get(current.user_id)
    return ProfileResponse(
        userId=view.user_id,
        accountId=view.account_id,
        displayName=view.display_name,
        createdAt=view.created_at,
    )


@router.patch(
    "",
    response_model=ProfileResponse,
    summary="Изменить профиль",
    description="Изменяет отображаемое имя. `displayName: null` — сбросить имя.",
)
async def patch_profile(
    current: CurrentUser,
    body: ProfileUpdateRequest,
    service: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    view = await service.update(current.user_id, body.displayName)
    return ProfileResponse(
        userId=view.user_id,
        accountId=view.account_id,
        displayName=view.display_name,
        createdAt=view.created_at,
    )
