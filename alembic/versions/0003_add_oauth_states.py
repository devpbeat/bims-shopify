"""add oauth_states table

Revision ID: 0003_oauth_states
Revises: 0002_sync_states_cols
Create Date: 2026-08-30 00:00:00.000000

Stores server-side OAuth state nonces for the Shopify authorization code
grant flow (see api/shopify_oauth.py). A DB-backed store (rather than an
in-memory dict) is required because the service may run multiple worker
processes, and the nonce created by /shopify/install must be readable by
whichever worker handles /shopify/callback.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_oauth_states"
down_revision: str | Sequence[str] | None = "0002_sync_states_cols"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shop", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state"),
    )
    with op.batch_alter_table("oauth_states", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oauth_states_shop"), ["shop"], unique=False)
        batch_op.create_index(batch_op.f("ix_oauth_states_state"), ["state"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("oauth_states", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oauth_states_state"))
        batch_op.drop_index(batch_op.f("ix_oauth_states_shop"))
    op.drop_table("oauth_states")
