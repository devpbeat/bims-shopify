"""tenants: auto-import flags for scheduled catalog import

Revision ID: 0013_tenant_auto_import
Revises: 0012_ops_jobs
Create Date: 2026-09-19 00:00:00.000000

Business context: new BIMS products currently only appear in Shopify after
someone manually triggers `catalog import` via the admin API/CLI. These
flags let the scheduler auto-enqueue that import per tenant on an interval,
without ever touching the existing inventory-sync scheduling.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_tenant_auto_import"
down_revision: str | Sequence[str] | None = "0012_ops_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("tenants")}

    if "auto_import_products" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column("auto_import_products", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "auto_import_publish" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column("auto_import_publish", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "auto_import_interval_minutes" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column(
                "auto_import_interval_minutes", sa.Integer(), nullable=False, server_default="360"
            ),
        )


def downgrade() -> None:
    op.drop_column("tenants", "auto_import_interval_minutes")
    op.drop_column("tenants", "auto_import_publish")
    op.drop_column("tenants", "auto_import_products")
