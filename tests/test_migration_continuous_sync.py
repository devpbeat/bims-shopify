"""Migrations 0023/0024: the push baseline and the push/reconcile/pull intervals are gone,
existing data survives, and the minimum pull gap starts at its default."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


def _run(url: str, action, revision: str) -> None:
    os.environ["ALEMBIC_DATABASE_URL"] = url
    try:
        action(_config(url), revision)
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)


def _filler(column: sa.Column):
    if column.default is not None or column.server_default is not None or column.nullable:
        return None
    kind = column.type
    if isinstance(kind, sa.Boolean):
        return False
    if isinstance(kind, sa.Integer):
        return 1
    if isinstance(kind, sa.Float | sa.Numeric):
        return 0.0
    if isinstance(kind, sa.JSON):
        return {}
    if isinstance(kind, sa.DateTime):
        return "2026-10-17 00:00:00"
    return "x"


def _insert(conn, table_name: str, **values) -> None:
    table = sa.Table(table_name, sa.MetaData(), autoload_with=conn)
    row = {c.name: _filler(c) for c in table.columns if c.name != "id" and _filler(c) is not None}
    row.update(values)
    conn.execute(table.insert().values(**row))


def test_upgrade_drops_the_baseline_and_the_intervals_and_keeps_the_data():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    sync_url = f"sqlite:///{db_file.name}"
    async_url = f"sqlite+aiosqlite:///{db_file.name}"
    try:
        _run(async_url, command.upgrade, "0022_self_healing_cycle")
        engine = sa.create_engine(sync_url)
        history = [
            {"at": "2026-10-17T00:00:00+00:00", "seconds": 600.0, "mode": "full"},
            {"at": "2026-10-17T00:10:00+00:00", "seconds": 4.0, "mode": "push"},
            {"at": "2026-10-17T00:20:00+00:00", "seconds": 240.0, "mode": "reconcile"},
        ]
        with engine.begin() as conn:
            _insert(
                conn, "tenants", slug="acme", sync_interval_minutes=15, push_interval_minutes=3,
                reconcile_interval_minutes=240,
            )
            _insert(
                conn, "sync_states", tenant_id=1, product_hashes={"A": "5"},
                last_push_outcome={"status": "ok"},
                last_reconcile_outcome={"status": "ok", "drift": {"count": 2}},
                sync_duration_history=history,
            )
        engine.dispose()

        _run(async_url, command.upgrade, "head")

        engine = sa.create_engine(sync_url)
        inspector = sa.inspect(engine)
        tenant_columns = {c["name"] for c in inspector.get_columns("tenants")}
        state_columns = {c["name"] for c in inspector.get_columns("sync_states")}
        assert {"sync_interval_minutes", "push_interval_minutes", "reconcile_interval_minutes"}.isdisjoint(
            tenant_columns
        )
        assert {"product_hashes", "last_push_outcome"}.isdisjoint(state_columns)
        assert "pull_min_gap_seconds" in tenant_columns
        with engine.connect() as conn:
            gap = conn.execute(sa.text("SELECT pull_min_gap_seconds, slug FROM tenants")).one()
            state = conn.execute(
                sa.text("SELECT last_reconcile_outcome, sync_duration_history FROM sync_states")
            ).one()
        assert tuple(gap) == (60, "acme")  # every tenant starts on the default gap
        assert json.loads(state[0])["drift"]["count"] == 2  # the write outcome survives
        kept = json.loads(state[1])
        assert [e["mode"] for e in kept] == ["full", "reconcile"]  # the removed push history is purged
        engine.dispose()

        _run(async_url, command.downgrade, "0022_self_healing_cycle")  # reversible
        engine = sa.create_engine(sync_url)
        columns = {c["name"] for c in sa.inspect(engine).get_columns("tenants")}
        assert {"sync_interval_minutes", "push_interval_minutes", "reconcile_interval_minutes"} <= columns
        assert "pull_min_gap_seconds" not in columns
        engine.dispose()
    finally:
        os.unlink(db_file.name)
