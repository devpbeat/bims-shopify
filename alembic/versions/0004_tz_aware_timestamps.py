"""tenants.bims_timezone + timezone-aware timestamp columns

Revision ID: 0004_tz_aware_timestamps
Revises: 0003_oauth_states
Create Date: 2026-08-30 00:00:00.000000

Two things:

1. Adds `tenants.bims_timezone` (IANA zone key, default "America/Asuncion")
   so each tenant's naive-local BIMS timestamps can be converted to/from
   aware UTC correctly (see `adapters.bims.timezones`).

2. Converts the naive `DateTime` columns that hold internal (non-BIMS)
   timestamps to timezone-aware:
   - sync_states.last_run_at, sync_states.last_error_at
   - oauth_states.created_at, oauth_states.expires_at
   - processed_events.created_at

   On Postgres, existing values in these columns were always written as
   naive UTC clock time (see the removed `_utcnow_naive` helpers), so the
   column is altered with `USING col AT TIME ZONE 'UTC'`, which reinterprets
   the existing naive values as UTC (not a blind cast, which would keep the
   clock digits but silently change their meaning to "local server tz").

   On SQLite, `ALTER COLUMN TYPE` isn't supported and, more importantly,
   SQLite has no real tz-aware column type at all — `DateTime(timezone=True)`
   silently stores/reads naive datetimes there regardless of what the
   Alembic migration does. So this step is a no-op on SQLite; the
   application handles it by re-tagging naive reads as UTC at the
   repository boundary (`datetime_utils.ensure_aware_utc`).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_tz_aware_timestamps"
down_revision: str | Sequence[str] | None = "0003_oauth_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ_COLUMNS = [
    ("sync_states", "last_run_at"),
    ("sync_states", "last_error_at"),
    ("oauth_states", "created_at"),
    ("oauth_states", "expires_at"),
    ("processed_events", "created_at"),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tenant_columns = {col["name"] for col in inspector.get_columns("tenants")}
    if "bims_timezone" not in tenant_columns:
        with op.batch_alter_table("tenants", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "bims_timezone",
                    sa.String(length=64),
                    nullable=False,
                    server_default="America/Asuncion",
                )
            )
        with op.batch_alter_table("tenants", schema=None) as batch_op:
            batch_op.alter_column("bims_timezone", server_default=None)

    if bind.dialect.name == "sqlite":
        # SQLite has no real tz-aware column type; DateTime(timezone=True)
        # already silently round-trips naive datetimes there, so there is
        # no schema change to make. See module docstring.
        return

    for table, column in _TZ_COLUMNS:
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITH TIME ZONE '
            f'USING "{column}" AT TIME ZONE \'UTC\''
        )


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("bims_timezone")

    if bind.dialect.name == "sqlite":
        return

    for table, column in _TZ_COLUMNS:
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITHOUT TIME ZONE '
            f'USING "{column}" AT TIME ZONE \'UTC\''
        )
