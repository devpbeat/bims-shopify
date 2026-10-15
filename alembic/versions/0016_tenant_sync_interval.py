"""tenants: per-tenant sync interval

Revision ID: 0016_tenant_sync_interval
Revises: 0015_tenant_sync_mode
Create Date: 2026-10-15 00:10:00.000000

The scheduled inventory sync interval used to be the global
`SYNC_INTERVAL_MINUTES` env var, which needed a redeploy to change. It is now
a tenant column (default 30, the previous global default) editable through
the tenant API; the running scheduler reschedules the tenant's job live.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_tenant_sync_interval"
down_revision: str | Sequence[str] | None = "0015_tenant_sync_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("tenants")}

    if "sync_interval_minutes" not in existing_columns:
        op.add_column(
            "tenants",
            sa.Column("sync_interval_minutes", sa.Integer(), nullable=False, server_default="30"),
        )


def downgrade() -> None:
    op.drop_column("tenants", "sync_interval_minutes")
