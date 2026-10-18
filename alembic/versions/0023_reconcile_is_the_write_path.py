"""sync_states: drop the push baseline and the push outcome

Revision ID: 0023_reconcile_is_the_write_path
Revises: 0022_self_healing_cycle
Create Date: 2026-10-18 12:00:00.000000

Stock is now written by exactly one path: every cycle reads Shopify's real
quantities and writes what differs from the snapshot. The "last pushed"
baseline (``product_hashes``) it replaces was a second notion of truth that
could drift away from Shopify and freeze a SKU, so it is removed instead of
left unused. ``last_push_outcome`` belonged to the removed push worker: the
cycle's outcome lives in ``last_reconcile_outcome``. The duration history of
the removed push worker is purged as well.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_reconcile_is_the_write_path"
down_revision: str | Sequence[str] | None = "0022_self_healing_cycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPPED = ("product_hashes", "last_push_outcome")


def _purge_push_history(bind) -> None:
    # Typed Core statements, not raw SQL: the JSON column is JSON on Postgres (production)
    # and TEXT-backed on SQLite (tests), and the type makes each dialect bind it correctly.
    states = sa.table(
        "sync_states", sa.column("id", sa.Integer), sa.column("sync_duration_history", sa.JSON)
    )
    for row_id, history in bind.execute(sa.select(states.c.id, states.c.sync_duration_history)):
        history = history or []
        kept = [e for e in history if e.get("mode") != "push"]
        if len(kept) != len(history):
            bind.execute(
                sa.update(states).where(states.c.id == row_id).values(sync_duration_history=kept)
            )


def upgrade() -> None:
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("sync_states")}
    _purge_push_history(bind)
    with op.batch_alter_table("sync_states") as batch:
        for name in _DROPPED:
            if name in existing:
                batch.drop_column(name)


def downgrade() -> None:
    with op.batch_alter_table("sync_states") as batch:
        batch.add_column(
            sa.Column("product_hashes", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(sa.Column("last_push_outcome", sa.JSON(), nullable=True))
