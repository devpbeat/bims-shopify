"""sync_states: latest pull and push outcomes

Revision ID: 0020_sync_worker_outcomes
Revises: 0019_tenant_worker_settings
Create Date: 2026-10-15 01:20:00.000000

Each worker tick records what it did (ok, skipped because the previous run was
still going, rejected by the sanity guard, declined for a stale snapshot, ...)
so a tick that did nothing is visible instead of silent.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_sync_worker_outcomes"
down_revision: str | Sequence[str] | None = "0019_tenant_worker_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_columns = {col["name"] for col in sa.inspect(bind).get_columns("sync_states")}

    if "last_pull_outcome" not in existing_columns:
        op.add_column("sync_states", sa.Column("last_pull_outcome", sa.JSON(), nullable=True))
    if "last_push_outcome" not in existing_columns:
        op.add_column("sync_states", sa.Column("last_push_outcome", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_states", "last_push_outcome")
    op.drop_column("sync_states", "last_pull_outcome")
