"""Wallet route: GET /v1/wallet.

ONE endpoint, read-only. There is deliberately no public consume/grant: a client that could debit
or credit itself would bypass policy, pricing and the generation ledger.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_wallet_service
from app.errors import RateLimitedError
from app.schemas.wallet import WalletResponse
from app.wallet.service import WalletService

router = APIRouter(prefix="/v1/wallet", tags=["Wallet"])


@router.get(
    "",
    response_model=WalletResponse,
    summary="Баланс кредитов",
    description=(
        "Текущий баланс кредитов пользователя. Кошелёк создаётся лениво — его отсутствие не "
        "ошибка, а `balance: 0`. Параметра «чей кошелёк» нет: баланс всегда только для `sub` из "
        "токена."
    ),
)
async def get_wallet(
    current: CurrentUser,
    wallet: Annotated[WalletService, Depends(get_wallet_service)],
) -> WalletResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    balance, updated_at = await wallet.get_wallet(current.user_id)
    return WalletResponse(balance=balance, updatedAt=updated_at)
