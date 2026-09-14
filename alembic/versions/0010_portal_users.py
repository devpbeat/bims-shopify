"""portal_users: email+password login for the merchant self-service portal

Revision ID: 0010_portal_users
Revises: 0009_audit_logs
Create Date: 2026-09-14 00:00:00.000000

Business context: alongside the existing per-tenant portal bearer token
(``portal_token_hash``, minted by an admin and shared like an access key),
tenants can now provision named per-user logins
(``POST /tenants/{slug}/portal-users``, admin-only) so individual store
staff sign in with their own email + password
(``POST /api/portal/{slug}/login``) rather than sharing one token. Email is
unique per tenant (not globally) via ``uq_portal_users_tenant_email``, so
two different stores may each have a user with the same email address.
Only the bcrypt hash is ever persisted (``password_hash``) — the plaintext
password is never stored or logged.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_portal_users"
down_revision: str | Sequence[str] | None = "0009_audit_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "portal_users" in inspector.get_table_names():
        return

    op.create_table(
        "portal_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_portal_users_tenant_email"),
    )
    with op.batch_alter_table("portal_users", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_portal_users_tenant_id"), ["tenant_id"], unique=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "portal_users" in inspector.get_table_names():
        with op.batch_alter_table("portal_users", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_portal_users_tenant_id"))
        op.drop_table("portal_users")
