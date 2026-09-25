"""Declarative base for ORM models.

Every CORE table has an ORM model bound to this Base. ``migrations/env.py`` uses
``target_metadata = Base.metadata``, so a table without a model would be seen as "extra in the
database" by ``alembic revision --autogenerate`` and emitted as ``op.drop_table(...)``. The
invariant is enforced by the ``compare_metadata()`` test (empty diff).
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
