"""tenants: minimum pull gap replaces the pull, push and reconcile intervals

Revision ID: 0024_continuous_pull_settings
Revises: 0023_reconcile_is_the_write_path
Create Date: 2026-10-18 12:30:00.000000

The pull no longer ticks on a fixed interval: it runs back to back, paced by its own
duration, with a per-tenant MINIMUM gap between pulls (``pull_min_gap_seconds``, 60 by
default) as the only knob. The write cycle runs once per new snapshot, so the push and
reconcile intervals mean nothing any more and are dropped together with the pull interval
instead of lingering as dead settings. Their values are not migrated: each described a
timer that no longer exists (a 30-minute pull interval is not a 30-minute gap), so every
tenant starts on the default gap and an operator edits it with a PATCH if needed.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_continuous_pull_settings"
down_revision: str | Sequence[str] | None = "0023_reconcile_is_the_write_path"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPPED = ("sync_interval_minutes", "push_interval_minutes", "reconcile_interval_minutes")
_DEFAULTS = {
    "sync_interval_minutes": "30",
    "push_interval_minutes": "5",
    "reconcile_interval_minutes": "480",
}


def upgrade() -> None:
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("tenants")}
    with op.batch_alter_table("tenants") as batch:
        if "pull_min_gap_seconds" not in existing:
            batch.add_column(
                sa.Column(
                    "pull_min_gap_seconds", sa.Integer(), nullable=False, server_default="60"
                )
            )
        for name in _DROPPED:
            if name in existing:
                batch.drop_column(name)


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        for name in _DROPPED:
            batch.add_column(
                sa.Column(name, sa.Integer(), nullable=False, server_default=_DEFAULTS[name])
            )
        batch.drop_column("pull_min_gap_seconds")
