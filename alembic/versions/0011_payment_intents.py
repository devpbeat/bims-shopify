"""payment_intents: persisted hosted-checkout payment links per Shopify order

Revision ID: 0011_payment_intents
Revises: 0010_portal_users
Create Date: 2026-09-19 00:00:00.000000

Business context: the Pagopar hosted-checkout flow creates a payment link
for a pending order (manual-payment Shopify orders) and later receives a
callback that never echoes the merchant's own order id — only Pagopar's
``hash_pedido``. Persisting the link lets the callback resolve which
Shopify order to mark paid, and lets the merchant portal list recent
payment links (``GET /api/portal/{slug}/payments``).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_payment_intents"
down_revision: str | Sequence[str] | None = "0010_portal_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "payment_intents" in inspector.get_table_names():
        return

    op.create_table(
        "payment_intents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("order_id", sa.String(255), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(10), nullable=False, server_default="PYG"),
        sa.Column("status", sa.String(40), nullable=False, server_default="pending"),
        sa.Column("checkout_url", sa.String(1024), nullable=True),
        sa.Column("provider_reference", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider", "order_id", name="uq_payment_intents_tenant_provider_order"),
    )
    with op.batch_alter_table("payment_intents", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_payment_intents_tenant_id"), ["tenant_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_payment_intents_provider_reference"), ["provider_reference"], unique=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "payment_intents" in inspector.get_table_names():
        with op.batch_alter_table("payment_intents", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_payment_intents_provider_reference"))
            batch_op.drop_index(batch_op.f("ix_payment_intents_tenant_id"))
        op.drop_table("payment_intents")
