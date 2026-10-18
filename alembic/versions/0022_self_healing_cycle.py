"""tenants: self-healing stage switches; sync_states: heal outcome and Shopify SKU index

Revision ID: 0022_self_healing_cycle
Revises: 0021_reconcile_worker
Create Date: 2026-10-17 18:00:00.000000

The scheduled push cycle now also creates the BIMS products and variants with
stock that the store lacks, and publishes the drafts that pass the publish
gates. Both stages are per-tenant switches, ON by default, so an operator can
turn one off with a PATCH instead of a deploy. The heal outcome records what
each stage did; the SKU index remembers which SKUs Shopify held at the last
scan so a cycle with nothing new costs no Shopify call.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_self_healing_cycle"
down_revision: str | Sequence[str] | None = "0021_reconcile_worker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tenant_columns = {col["name"] for col in inspector.get_columns("tenants")}
    state_columns = {col["name"] for col in inspector.get_columns("sync_states")}

    for name in ("auto_create_missing", "auto_publish"):
        if name not in tenant_columns:
            op.add_column(
                "tenants",
                sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()),
            )
    for name in ("last_heal_outcome", "shopify_sku_index"):
        if name not in state_columns:
            op.add_column("sync_states", sa.Column(name, sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_states", "shopify_sku_index")
    op.drop_column("sync_states", "last_heal_outcome")
    op.drop_column("tenants", "auto_publish")
    op.drop_column("tenants", "auto_create_missing")
