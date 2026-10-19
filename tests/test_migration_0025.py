"""Migration 0025: nullable Fernet column for the Bancard vPOS private key."""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[1]
COLUMN = "bancard_vpos_private_key_encrypted"


def _run(url: str, action, revision: str) -> None:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = url.replace("sqlite://", "sqlite+aiosqlite://")
    try:
        action(cfg, revision)
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)


def _columns(url: str) -> set[str]:
    engine = sa.create_engine(url)
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns("tenants")}
    finally:
        engine.dispose()


def _load_migration():
    path = REPO_ROOT / "alembic" / "versions" / "0025_bancard_vpos_private_key.py"
    spec = importlib.util.spec_from_file_location("migration_0025", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db_url() -> tuple[str, str]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    return f"sqlite:///{db_file.name}", db_file.name


def test_upgrade_adds_the_column_and_downgrade_drops_it():
    url, path = _db_url()
    try:
        _run(url, command.upgrade, "head")
        assert COLUMN in _columns(url)
        _run(url, command.downgrade, "-1")
        assert COLUMN not in _columns(url)
    finally:
        os.unlink(path)


def test_revision_chain():
    module = _load_migration()
    assert module.revision == "0025_bancard_vpos_private_key"
    assert module.down_revision == "0024_continuous_pull_settings"


def test_upgrade_is_idempotent_when_the_column_exists():
    url, path = _db_url()
    try:
        _run(url, command.upgrade, "head")
        module = _load_migration()
        engine = sa.create_engine(url)
        try:
            with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
                module.upgrade()
        finally:
            engine.dispose()
        assert COLUMN in _columns(url)
    finally:
        os.unlink(path)
