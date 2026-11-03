"""catalog_snapshot_stock: BIMS's unfiltered total next to the per-warehouse detail

Revision ID: 0027_snapshot_unfiltered_stock
Revises: 0026_payment_intent_process_id
Create Date: 2026-11-02 12:00:00.000000

BIMS's ``warehouse_ids`` filter can answer 0 for stock that exists, so the pull
now also asks for the total WITHOUT the filter and keeps it beside the
per-warehouse breakdown (``by_warehouse``). Nullable: snapshots written before
this revision, and SKUs BIMS did not answer for, have no unfiltered figure
(unknown, never zero).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_snapshot_unfiltered_stock"
down_revision: str | Sequence[str] | None = "0026_payment_intent_process_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "catalog_snapshot_stock"
_COLUMN = "unfiltered_quantity"


def upgrade() -> None:
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in existing:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
