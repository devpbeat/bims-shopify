"""add missing sync_states error/summary columns (idempotent, legacy-safe)

Revision ID: 0002_sync_states_cols
Revises: 6376be5c801c
Create Date: 2026-08-29 00:00:00.000000

This migration exists for pre-Alembic Postgres databases that already had
the `sync_states` table (created via `Base.metadata.create_all`) but are
missing the `last_error`, `last_error_at`, and `last_run_summary` columns
that were added to `SyncStateModel` later.

For a brand new database, revision 6376be5c801c ("initial schema") already
creates `sync_states` with these columns, so this migration is a no-op
there (the inspector will find the columns already present).

For a legacy database, operators must stamp the DB to 6376be5c801c first
(since the tables already exist and 0001 must be skipped), then run
`alembic upgrade head` so this migration adds only the missing columns.
See README.md "Database Migrations" for the exact commands.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_sync_states_cols"
down_revision: str | Sequence[str] | None = "6376be5c801c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_COLUMNS = {
    "last_error": lambda: sa.Column("last_error", sa.String(length=2000), nullable=True),
    "last_error_at": lambda: sa.Column("last_error_at", sa.DateTime(), nullable=True),
    "last_run_summary": lambda: sa.Column(
        "last_run_summary", sa.JSON(), nullable=False, server_default="{}"
    ),
}


def upgrade() -> None:
    """Add any of the new sync_states columns that are not already present."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("sync_states")}

    with op.batch_alter_table("sync_states", schema=None) as batch_op:
        for name, column_factory in _NEW_COLUMNS.items():
            if name not in existing_columns:
                batch_op.add_column(column_factory())

    # Drop the server_default after backfill so it matches the ORM model,
    # which manages the default in Python (default=dict) rather than in the DB.
    if "last_run_summary" not in existing_columns:
        with op.batch_alter_table("sync_states", schema=None) as batch_op:
            batch_op.alter_column("last_run_summary", server_default=None)


def downgrade() -> None:
    """Drop the columns added by this migration, if present."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("sync_states")}

    with op.batch_alter_table("sync_states", schema=None) as batch_op:
        for name in _NEW_COLUMNS:
            if name in existing_columns:
                batch_op.drop_column(name)
