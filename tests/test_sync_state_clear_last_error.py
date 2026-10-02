"""Unit test for clearing a stale last_error on a successful sync.

Regression guard for the bug where sync_states.last_error/last_error_at
were never cleared after a successful run, so the portal kept surfacing an
old error (e.g. a NO_DUPLICATE from weeks ago) even though recent syncs
succeeded.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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


async def test_clear_last_error_resets_both_fields(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        await repo.set_last_error(1, "NO_DUPLICATE: boom")

    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        status = await repo.get_status(1)
    assert status["last_error"] == "NO_DUPLICATE: boom"
    assert status["last_error_at"] is not None

    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        await repo.clear_last_error(1)

    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        status = await repo.get_status(1)
    assert status["last_error"] is None
    assert status["last_error_at"] is None


async def test_clear_last_error_is_noop_when_no_error_set(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        await repo.clear_last_error(2)
        status = await repo.get_status(2)
    assert status["last_error"] is None
    assert status["last_error_at"] is None
