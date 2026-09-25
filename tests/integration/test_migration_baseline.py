"""The single baseline migration is reversible AND agrees with the ORM.

``compare_metadata()`` returning an empty diff is what makes ``alembic revision --autogenerate``
safe for a domain: a core table missing from ``Base.metadata`` would otherwise be emitted as
``op.drop_table(...)`` — which is exactly how ``auth_devices`` (the deviceId→userId mapping the
payment webhooks resolve users with) came within one autogenerate of being dropped.

These tests are deliberately SYNCHRONOUS: ``migrations/env.py`` drives its own ``asyncio.run()``,
which cannot be nested inside a running event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base
from tests.conftest import alembic_config, reenable_loggers

# Artefacts that are not part of the core schema: the two views (raw SQL in the baseline — Alembic
# does not track views) and the table the DomainRegistry wiring test creates.
_NOT_CORE = {"v_generations_daily", "v_generations_user_totals", "demo_domain_rows"}


@pytest.fixture
def alembic_cfg(migrated: str) -> Config:
    """No ini file on purpose — see ``tests.conftest.alembic_config`` (fileConfig would disable
    every existing logger and turn the log-level assertions of the whole suite into no-ops)."""
    cfg: Config = alembic_config(migrated)
    yield cfg
    reenable_loggers()


def _include_object(obj: Any, name: str, type_: str, reflected: bool, compare_to: Any) -> bool:
    return not (type_ == "table" and name in _NOT_CORE)


def _diff(sync_connection: Any) -> list[Any]:
    context = MigrationContext.configure(
        sync_connection, opts={"include_object": _include_object, "compare_type": False}
    )
    return compare_metadata(context, Base.metadata)


async def _collect_diff(url: str) -> list[Any]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_diff)
    finally:
        await engine.dispose()


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    return {r[0] for r in rows}


async def _views(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.views "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    return {r[0] for r in rows}


def test_upgrade_downgrade_upgrade_is_reproducible(alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")


def test_orm_metadata_matches_the_database(alembic_cfg: Config, migrated: str) -> None:
    """The diff must be EMPTY: the schema in the DB is exactly the schema the models describe."""
    command.upgrade(alembic_cfg, "head")
    diff = asyncio.run(_collect_diff(migrated))
    assert diff == [], f"the ORM and the database disagree: {diff}"


def test_every_core_table_has_an_orm_model(migrated: str) -> None:
    in_db = asyncio.run(_tables(migrated)) - {"alembic_version"} - _NOT_CORE
    in_orm = set(Base.metadata.tables)
    assert in_db == in_orm, f"only in DB: {in_db - in_orm}; only in ORM: {in_orm - in_db}"


def test_views_exist(migrated: str) -> None:
    assert {"v_generations_daily", "v_generations_user_totals"} <= asyncio.run(_views(migrated))
