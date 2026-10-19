"""tenants: encrypted Bancard vPOS private key

Revision ID: 0025_bancard_vpos_private_key
Revises: 0024_continuous_pull_settings
Create Date: 2026-10-19 16:00:00.000000

Adds the nullable, Fernet-encrypted column that holds a tenant's Bancard vPOS private key.
It is nullable because only tenants using the direct vPOS rail have one; every existing
tenant keeps NULL and loads unchanged. The public key and environment live in
``provider_config``; the private key never does.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_bancard_vpos_private_key"
down_revision: str | Sequence[str] | None = "0024_continuous_pull_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "bancard_vpos_private_key_encrypted"


def upgrade() -> None:
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("tenants")}
    if _COLUMN in existing:
        return
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(sa.Column(_COLUMN, sa.String(1024), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column(_COLUMN)
