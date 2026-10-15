"""tenants: push cadence, snapshot staleness limit and pull sanity ratio

Revision ID: 0019_tenant_worker_settings
Revises: 0018_catalog_snapshot
Create Date: 2026-10-15 01:10:00.000000

With the sync split into a PULL worker (`sync_interval_minutes`, unchanged) and
a PUSH worker, the push needs its own cadence, a staleness limit beyond which
it refuses to push from the snapshot, and the pull needs the row-count ratio
below which a new pull is rejected as implausible. All per tenant, all editable
through the tenant API and applied without a restart.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_tenant_worker_settings"
down_revision: str | Sequence[str] | None = "0018_catalog_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_columns = {col["name"] for col in sa.inspect(bind).get_columns("tenants")}

    if "push_interval_minutes" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column("push_interval_minutes", sa.Integer(), nullable=False, server_default="5"),
        )
    if "snapshot_max_age_minutes" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column("snapshot_max_age_minutes", sa.Integer(), nullable=False, server_default="120"),
        )
    if "pull_min_ratio" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column("pull_min_ratio", sa.Float(), nullable=False, server_default="0.3"),
        )


def downgrade() -> None:
    op.drop_column("tenants", "pull_min_ratio")
    op.drop_column("tenants", "snapshot_max_age_minutes")
    op.drop_column("tenants", "push_interval_minutes")
