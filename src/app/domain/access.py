"""Access decisions for the domain — thin layer over the core policy.

The SAME ``effective()`` the core generation uses (one decision function: the paywall can never
say «можно» while the run says «blocked»). Checked twice: when the request is accepted (so the
user sees the paywall immediately and nothing is queued) and again inside
``GenerationService.run()`` in the worker (the balance may have changed meanwhile).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.config import DomainSettings
from app.domain.models import ChatMessage
from app.domain.pricing import DomainPricing
from app.policy.loader import EffectivePolicy, effective


async def check(
    session: AsyncSession, user_id: uuid.UUID, kind: str, *, price: int | None = None
) -> EffectivePolicy:
    """``price``: the exact credits of THIS request (a summary depends on the volume)."""
    if price is None:
        price = DomainPricing().quote(kind=kind, model=None, params={})
    return await effective(
        session, user_id, required_credits=max(price, 1), ctx={"kind": kind, "model": None}
    )


def block_reason(policy: EffectivePolicy) -> str | None:
    if policy.allowed:
        return None
    return policy.reasons[0].value if policy.reasons else "policy_denied"


async def demo_free_left(
    session: AsyncSession, user_id: uuid.UUID, settings: DomainSettings
) -> int:
    used = await session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.user_id == user_id, ChatMessage.is_demo_free.is_(True))
    )
    return max(settings.demo_free_questions - int(used or 0), 0)
