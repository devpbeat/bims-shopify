"""tenants.bims_warehouse_ids: aggregate stock across multiple warehouses

Revision ID: 0005_bims_warehouse_ids
Revises: 0004_tz_aware_timestamps
Create Date: 2026-09-14 00:00:00.000000

Adds `tenants.bims_warehouse_ids` (JSON list of int, default empty list).

Semantics: when non-empty, stock lookups (`BIMSERPAdapter.fetch_stock_levels`)
aggregate stock across all listed warehouse ids via BIMS's stock_fenicio
endpoint. When empty (the default, and the value for every pre-existing
tenant), lookups fall back to the single `bims_warehouse_id` column, so
existing single-warehouse tenants are unaffected. `bims_warehouse_id` is
kept as-is; it remains the default warehouse for purchase/reorder flows.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_bims_warehouse_ids"
down_revision: str | Sequence[str] | None = "0004_tz_aware_timestamps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tenant_columns = {col["name"] for col in inspector.get_columns("tenants")}
    if "bims_warehouse_ids" not in tenant_columns:
        with op.batch_alter_table("tenants", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "bims_warehouse_ids",
                    sa.JSON(),
                    nullable=False,
                    server_default="[]",
                )
            )
        with op.batch_alter_table("tenants", schema=None) as batch_op:
            batch_op.alter_column("bims_warehouse_ids", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("bims_warehouse_ids")
