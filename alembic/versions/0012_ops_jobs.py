"""ops_jobs: background catalog/rekey ops jobs triggered via the admin API

Revision ID: 0012_ops_jobs
Revises: 0011_payment_intents
Create Date: 2026-09-19 00:00:00.000000

Business context: catalog status/wipe/import/dedupe and rekey_skus were
CLI-only, requiring an SSH session to run. This table persists each run
(command, options, live progress, final result/error) so an admin can
trigger and monitor them via `/ops/{slug}/run` without shell access.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_ops_jobs"
down_revision: str | Sequence[str] | None = "0011_payment_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "ops_jobs" in inspector.get_table_names():
        return

    op.create_table(
        "ops_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("command", sa.String(40), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("progress_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ops_jobs_tenant_created", "ops_jobs", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ops_jobs_tenant_created", table_name="ops_jobs")
    op.drop_table("ops_jobs")
