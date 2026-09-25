"""Profile: ``displayName`` + the derived ``accountId``.

The ``user_profiles`` row is created LAZILY (upsert on the first PATCH). Its absence is not an error
— defaults are returned, never a 404.

``display_name`` deliberately does NOT live in ``users``: ``users`` is a CORE table, and a product
field in it would force every domain either to tolerate a foreign column or to patch the core.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.profile.account_id import account_id


@dataclass(frozen=True)
class ProfileView:
    user_id: uuid.UUID
    account_id: str
    display_name: str | None
    created_at: datetime.datetime | None


class ProfileService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> ProfileView:
        row = (
            await self._session.execute(
                text(
                    "SELECT p.display_name, u.created_at FROM users u "
                    "LEFT JOIN user_profiles p ON p.user_id = u.id WHERE u.id = :uid"
                ),
                {"uid": str(user_id)},
            )
        ).first()
        return ProfileView(
            user_id=user_id,
            account_id=account_id(user_id),  # always computed, never read from the DB
            display_name=row[0] if row else None,
            created_at=row[1] if row else None,
        )

    async def update(self, user_id: uuid.UUID, display_name: str | None) -> ProfileView:
        """Upsert. ``display_name=None`` clears the name (an explicit reset, not a no-op)."""
        await self._session.execute(
            text(
                "INSERT INTO user_profiles (user_id, display_name, updated_at) "
                "VALUES (:uid, :name, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "display_name = EXCLUDED.display_name, updated_at = now()"
            ),
            {"uid": str(user_id), "name": display_name},
        )
        return await self.get(user_id)
