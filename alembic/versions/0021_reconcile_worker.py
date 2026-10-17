"""tenants: reconcile cadence; sync_states: latest reconcile outcome

Revision ID: 0021_reconcile_worker
Revises: 0020_sync_worker_outcomes
Create Date: 2026-10-17 12:00:00.000000

The push worker diffs the snapshot against a stored baseline of what it last
pushed, not against what Shopify holds, so a SKU whose Shopify quantity drifted
away from that baseline is never corrected. The reconcile worker compares
Shopify's REAL quantities with the snapshot on its own (slow) cadence and
records what it found under its own outcome, apart from the push outcome.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_reconcile_worker"
down_revision: str | Sequence[str] | None = "0020_sync_worker_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tenant_columns = {col["name"] for col in inspector.get_columns("tenants")}
    state_columns = {col["name"] for col in inspector.get_columns("sync_states")}

    if "reconcile_interval_minutes" not in tenant_columns:
        op.add_column(
            "tenants",
            sa.Column(
                "reconcile_interval_minutes", sa.Integer(), nullable=False, server_default="480"
            ),
        )
    if "last_reconcile_outcome" not in state_columns:
        op.add_column("sync_states", sa.Column("last_reconcile_outcome", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_states", "last_reconcile_outcome")
    op.drop_column("tenants", "reconcile_interval_minutes")
