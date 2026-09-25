"""Schemas of ``GET /v1/wallet``.

There is NO consume/grant request schema here — and that is the contract, not an omission: no
public endpoint moves money. A debit is a consequence of a generation; a credit is a consequence
of a verified payment.
"""

from __future__ import annotations

import datetime

from pydantic import Field

from app.schemas.common import StrictModel


class WalletResponse(StrictModel):
    balance: int = Field(description="Текущий баланс кредитов. Кошелька нет → `0` (не ошибка).")
    updatedAt: datetime.datetime | None = Field(
        default=None,
        description="Когда баланс менялся последний раз. `null`, если операций ещё не было.",
    )
