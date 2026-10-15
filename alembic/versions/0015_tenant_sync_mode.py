"""tenants: scheduled sync mode (full | incremental)

Revision ID: 0015_tenant_sync_mode
Revises: 0014_auto_import_stock_only
Create Date: 2026-10-15 00:00:00.000000

Business context: BIMS's `last_update` filter proved unreliable (consecutive
incremental runs missed changed products, and thousands of products kept a
stale zero in Shopify because their BIMS record had not changed since the
last push). With bounded concurrency a full catalog comparison takes a few
minutes, so the scheduled sync now compares the FULL catalog every run.
Defaults to 'full', including for existing tenants; an operator can switch a
tenant back to 'incremental' through the tenant API without a deploy.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_tenant_sync_mode"
down_revision: str | Sequence[str] | None = "0014_auto_import_stock_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("tenants")}

    if "sync_mode" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column("sync_mode", sa.String(16), nullable=False, server_default="full"),
        )


def downgrade() -> None:
    op.drop_column("tenants", "sync_mode")
