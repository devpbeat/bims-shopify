"""rekey_reports: persist rekey_skus scan output per tenant

Revision ID: 0007_rekey_reports
Revises: 0006_push_orders_to_bims
Create Date: 2026-09-14 00:00:00.000000

Business context: the ``rekey_skus`` ops script scans Shopify variants
against the BIMS company-6 SKU catalog and reports duplicates, name
mismatches, and unresolved SKUs to store staff for manual triage (via an
xlsx export). Each run's full result is also persisted here so the
history of scans is queryable/auditable without relying on stdout logs,
which are lost once the CLI process exits. Standalone-mode runs have no
tenant record, so ``tenant_id`` is nullable and only DB-mode runs write a
row here.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_rekey_reports"
down_revision: str | Sequence[str] | None = "0006_push_orders_to_bims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "rekey_reports" in inspector.get_table_names():
        return

    op.create_table(
        "rekey_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("rekey_reports", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_rekey_reports_tenant_id"), ["tenant_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("rekey_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_rekey_reports_tenant_id"))
    op.drop_table("rekey_reports")
