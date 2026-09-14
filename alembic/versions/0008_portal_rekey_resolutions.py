"""tenants.portal_token_hash + rekey_resolutions: merchant portal auth and per-variant decisions

Revision ID: 0008_portal_rekey_resolutions
Revises: 0007_rekey_reports
Create Date: 2026-09-14 00:00:00.000000

Business context: the merchant self-service portal
(``/api/portal/{slug}``) lets store staff review a tenant's latest
``rekey_reports`` scan and decide, per variant, whether to keep it,
delete it, approve a proposed SKU rewrite, or ignore it. Access is
gated by a per-tenant bearer token (``portal_token_hash``, minted by an
admin via ``POST /tenants/{slug}/portal-token``); only the SHA-256 hash
is stored, never the plaintext. Each portal decision is persisted in
``rekey_resolutions`` so re-posting the same decision is detected as
idempotent (unique on ``(report_id, variant_id)``) instead of being
re-applied against Shopify.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_portal_rekey_resolutions"
down_revision: str | Sequence[str] | None = "0007_rekey_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    tenant_columns = {col["name"] for col in inspector.get_columns("tenants")}
    if "portal_token_hash" not in tenant_columns:
        with op.batch_alter_table("tenants", schema=None) as batch_op:
            batch_op.add_column(sa.Column("portal_token_hash", sa.String(64), nullable=True))

    if "rekey_resolutions" not in inspector.get_table_names():
        op.create_table(
            "rekey_resolutions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("report_id", sa.Integer(), nullable=False),
            sa.Column("variant_id", sa.String(255), nullable=False),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("status", sa.String(40), nullable=False),
            sa.Column("error", sa.String(2000), nullable=True),
            sa.Column("note", sa.String(2000), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.ForeignKeyConstraint(["report_id"], ["rekey_reports.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "report_id", "variant_id", name="uq_rekey_resolutions_report_variant"
            ),
        )
        with op.batch_alter_table("rekey_resolutions", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_rekey_resolutions_tenant_id"), ["tenant_id"], unique=False
            )
            batch_op.create_index(
                batch_op.f("ix_rekey_resolutions_report_id"), ["report_id"], unique=False
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "rekey_resolutions" in inspector.get_table_names():
        with op.batch_alter_table("rekey_resolutions", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_rekey_resolutions_report_id"))
            batch_op.drop_index(batch_op.f("ix_rekey_resolutions_tenant_id"))
        op.drop_table("rekey_resolutions")

    tenant_columns = {col["name"] for col in inspector.get_columns("tenants")}
    if "portal_token_hash" in tenant_columns:
        with op.batch_alter_table("tenants", schema=None) as batch_op:
            batch_op.drop_column("portal_token_hash")
