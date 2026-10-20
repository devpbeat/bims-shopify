"""payment_intents: Bancard vPOS process_id of the live attempt

Revision ID: 0026_payment_intent_process_id
Revises: 0025_bancard_vpos_private_key
Create Date: 2026-10-20 09:00:00.000000

The dynamic payment link mints the vPOS operation when the buyer opens the page, so the
``process_id`` returned by ``single_buy`` must be stored to be reused on a refresh. It is
nullable: rows for other providers, and intents prepared but not yet opened, have none.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_payment_intent_process_id"
down_revision: str | Sequence[str] | None = "0025_bancard_vpos_private_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "process_id"


def upgrade() -> None:
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("payment_intents")}
    if _COLUMN in existing:
        return
    with op.batch_alter_table("payment_intents") as batch:
        batch.add_column(sa.Column(_COLUMN, sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("payment_intents") as batch:
        batch.drop_column(_COLUMN)
