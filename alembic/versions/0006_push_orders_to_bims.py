"""tenants.push_orders_to_bims: per-tenant order push toggle

Revision ID: 0006_push_orders_to_bims
Revises: 0005_bims_warehouse_ids
Create Date: 2026-09-14 00:00:00.000000

Business context: for some tenants (e.g. mystore) BIMS is the single source
of truth for sales/stock — staff invoice manually in BIMS and the periodic
sync feeds Shopify inventory from BIMS. For those tenants, Shopify order
webhooks must NOT create a Sale in BIMS or trigger reorder logic.

Adds `tenants.push_orders_to_bims` (boolean, default true) so existing
tenants keep today's behavior (push orders to BIMS) unless explicitly opted
out.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_push_orders_to_bims"
down_revision: str | Sequence[str] | None = "0005_bims_warehouse_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tenant_columns = {col["name"] for col in inspector.get_columns("tenants")}
    if "push_orders_to_bims" not in tenant_columns:
        with op.batch_alter_table("tenants", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "push_orders_to_bims",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )
        with op.batch_alter_table("tenants", schema=None) as batch_op:
            batch_op.alter_column("push_orders_to_bims", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("push_orders_to_bims")
