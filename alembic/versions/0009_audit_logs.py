"""audit_logs: append-only audit trail for admin/portal/system/shopify actions

Revision ID: 0009_audit_logs
Revises: 0008_portal_rekey_resolutions
Create Date: 2026-09-14 00:00:00.000000

Business context: every mutating action taken through the admin API, the
merchant self-service portal, the Shopify OAuth install flow, the periodic
inventory sync, and inbound Shopify webhooks is recorded here for
traceability. Writing an audit entry must never break the business
operation it describes (see ``AuditLogger`` / ``SqlAlchemyAuditLogger``),
so this table is intentionally schema-light: a nullable FK to ``tenants``
(some events, like a scheduler-wide summary, are not tenant-scoped),
free-form ``actor``/``action``/``entity`` strings rather than enums (new
event types must not require a migration), and a compact JSON ``payload``
that is documented (not enforced) to hold only counts/ids — never secrets,
tokens, or API keys.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_audit_logs"
down_revision: str | Sequence[str] | None = "0008_portal_rekey_resolutions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "audit_logs" in inspector.get_table_names():
        return

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(40), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("entity", sa.String(120), nullable=False),
        sa.Column("entity_id", sa.String(255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_audit_logs_tenant_id_created_at"),
            ["tenant_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "audit_logs" in inspector.get_table_names():
        with op.batch_alter_table("audit_logs", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_audit_logs_tenant_id_created_at"))
        op.drop_table("audit_logs")
