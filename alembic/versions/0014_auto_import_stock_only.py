"""tenants: auto-import only-with-stock flag

Revision ID: 0014_auto_import_stock_only
Revises: 0013_tenant_auto_import
Create Date: 2026-09-25 00:00:00.000000

Business context: the client doesn't want dead, no-stock BIMS products
flooding Shopify (they can't be deleted there once created). Scheduled
auto-import now defaults to only importing product groups that have stock
in at least one of the tenant's warehouses, reusing the existing
`catalog import --only-with-stock` behavior. Defaults to TRUE per the
client's request, including for existing tenants.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_auto_import_stock_only"
down_revision: str | Sequence[str] | None = "0013_tenant_auto_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("tenants")}

    if "auto_import_only_with_stock" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column(
                "auto_import_only_with_stock", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
        )


def downgrade() -> None:
    op.drop_column("tenants", "auto_import_only_with_stock")
