"""Repository-boundary timezone normalization against a real (sqlite) DB.

SQLAlchemy's `DateTime(timezone=True)` silently stores/reads naive datetimes
on SQLite (it has no real tz-aware column type). These tests confirm that
legacy naive rows (simulating pre-migration data, or just SQLite's own
lossy round-trip) are always normalized to aware UTC by the time they reach
the application, and that comparing a repository-read value against
`datetime.now(UTC)` never raises `TypeError`.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.models import SyncStateModel
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)


@pytest.fixture
async def session_factory():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    database_url = f"sqlite+aiosqlite:///{db_file.name}"
    engine = create_async_engine(database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from alembic.config import Config

    from alembic import command

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    try:
        yield factory
    finally:
        await engine.dispose()
        Path(db_file.name).unlink(missing_ok=True)


async def test_legacy_naive_last_run_at_is_read_back_as_aware_utc(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        # Force-create the row, then simulate a legacy naive value being
        # already present in the DB (as if written before this migration,
        # or round-tripped naive by SQLite regardless of column type).
        await repo.set_last_run(1, datetime.now(UTC))
        legacy_naive = datetime.fromisoformat("2025-01-01T10:00:00")
        await session.execute(
            update(SyncStateModel)
            .where(SyncStateModel.tenant_id == 1)
            .values(last_run_at=legacy_naive)
        )
        await session.commit()

    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        last_run = await repo.get_last_run(1)

    assert last_run is not None
    assert last_run.tzinfo is not None

    # Must not raise "can't compare offset-naive and offset-aware datetimes".
    assert last_run < datetime.now(UTC)


async def test_get_status_serializes_aware_iso8601_with_offset(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        now = datetime.now(UTC)
        await repo.set_last_run(2, now)
        await repo.set_last_error(2, "boom")

    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        status = await repo.get_status(2)

    # An aware isoformat() string always carries an explicit UTC offset
    # (e.g. "+00:00"), unlike a naive one.
    assert status["last_run_at"].endswith("+00:00")
    assert status["last_error_at"].endswith("+00:00")
