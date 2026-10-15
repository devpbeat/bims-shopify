"""sync_states: recent sync durations

Revision ID: 0017_sync_duration_history
Revises: 0016_tenant_sync_interval
Create Date: 2026-10-15 00:20:00.000000

Persists the duration of recent completed syncs so a minimum safe scheduler
interval can be derived from measurements (see
application/sync_interval.py) and enforced by the tenant API.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_sync_duration_history"
down_revision: str | Sequence[str] | None = "0016_tenant_sync_interval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("sync_states")}

    if "sync_duration_history" not in existing_columns:
        op.add_column(
            "sync_states",
            sa.Column("sync_duration_history", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    op.drop_column("sync_states", "sync_duration_history")
