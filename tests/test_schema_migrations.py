"""Schema-drift detection: migrations must fully describe the ORM models.

If `models.py` changes without a corresponding Alembic migration, Alembic's
own autogenerate comparison will detect the difference. Running that
comparison here, in CI, against a freshly-migrated database turns "someone
forgot to generate a migration" into a failing test instead of a
production 500 (`UndefinedColumnError`) the next time the app boots against
a database that already has the old schema applied.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from alembic import command
from bims_shopify.adapters.persistence.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_models_match_migration_head() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    database_url = f"sqlite:///{db_file.name}"

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.name}"
    try:
        command.upgrade(cfg, "head")

        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                migration_context = MigrationContext.configure(connection)
                diff = compare_metadata(migration_context, Base.metadata)
        finally:
            engine.dispose()
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)
        os.unlink(db_file.name)

    assert diff == [], (
        "Base.metadata (models.py) has drifted from the Alembic migration "
        f"head. Run `make revision msg=\"...\"` to capture the change. Diff: {diff}"
    )
