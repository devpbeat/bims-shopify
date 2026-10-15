"""catalog snapshot: persisted copy of what BIMS reported

Revision ID: 0018_catalog_snapshot
Revises: 0017_sync_duration_history
Create Date: 2026-10-15 01:00:00.000000

The inventory sync is split into a PULL worker (BIMS -> our database, as slow
as BIMS is) and a PUSH worker (our database -> Shopify, seconds). This is the
hand-off between them: per tenant, a snapshot of the product rows, the stock
per SKU (aggregated AND per warehouse) and the pull metadata.

A pull is written as a 'building' generation that no reader looks at, then
made visible by flipping `is_current` in one short transaction, so a reader
never sees a half-written snapshot and a process killed mid-write leaves only
an invisible orphan. The partial unique index guarantees one current snapshot
per tenant.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_catalog_snapshot"
down_revision: str | Sequence[str] | None = "0017_sync_duration_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "catalog_snapshots" not in existing:
        op.create_table(
            "catalog_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rows_pulled", sa.Integer(), nullable=False),
            sa.Column("stock_rows", sa.Integer(), nullable=False),
            sa.Column("pages", sa.Integer(), nullable=False),
            sa.Column("reported_count", sa.Integer(), nullable=True),
            sa.Column("complete", sa.Boolean(), nullable=True),
            sa.Column("stop_reason", sa.String(32), nullable=False),
            sa.Column("warnings", sa.JSON(), nullable=False),
            sa.Column("timings", sa.JSON(), nullable=False),
            sa.Column("stats", sa.JSON(), nullable=True),
            sa.Column("rejection", sa.JSON(), nullable=True),
        )
        op.create_index("ix_catalog_snapshots_tenant_id", "catalog_snapshots", ["tenant_id"])
        op.create_index(
            "uq_catalog_snapshots_current",
            "catalog_snapshots",
            ["tenant_id"],
            unique=True,
            sqlite_where=sa.text("is_current"),
            postgresql_where=sa.text("is_current"),
        )

    if "catalog_snapshot_products" not in existing:
        op.create_table(
            "catalog_snapshot_products",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "snapshot_id",
                sa.Integer(),
                sa.ForeignKey("catalog_snapshots.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("bims_id", sa.String(64), nullable=False),
            sa.Column("sku", sa.String(255), nullable=True),
            sa.Column("code2", sa.String(255), nullable=True),
            sa.Column("name", sa.String(500), nullable=False),
            sa.Column("sell_price", sa.Float(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("exclude_ecommerce", sa.Boolean(), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_catalog_snapshot_products_snapshot_id", "catalog_snapshot_products", ["snapshot_id"]
        )

    if "catalog_snapshot_stock" not in existing:
        op.create_table(
            "catalog_snapshot_stock",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "snapshot_id",
                sa.Integer(),
                sa.ForeignKey("catalog_snapshots.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sku", sa.String(255), nullable=False),
            sa.Column("quantity", sa.Float(), nullable=False),
            sa.Column("by_warehouse", sa.JSON(), nullable=False),
            sa.UniqueConstraint("snapshot_id", "sku"),
        )
        op.create_index(
            "ix_catalog_snapshot_stock_snapshot_id", "catalog_snapshot_stock", ["snapshot_id"]
        )


def downgrade() -> None:
    op.drop_table("catalog_snapshot_stock")
    op.drop_table("catalog_snapshot_products")
    op.drop_table("catalog_snapshots")
