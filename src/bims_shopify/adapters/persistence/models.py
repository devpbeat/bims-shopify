"""SQLAlchemy ORM models."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now_utc() -> datetime:
    """Aware UTC "now" used as a column default.

    On Postgres these columns are `DateTime(timezone=True)` (timestamptz), so
    the aware value round-trips as-is. On SQLite, `DateTime(timezone=True)`
    silently stores/reads naive datetimes (SQLite has no real tz-aware
    column type); values read back there are re-tagged as UTC via
    `datetime_utils.ensure_aware_utc` at the repository boundary rather than
    stripped here.
    """
    return datetime.now(UTC)


class TenantModel(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    bims_base_url: Mapped[str] = mapped_column(String(255))
    bims_api_key_encrypted: Mapped[str] = mapped_column(String(1024))
    shopify_shop_domain: Mapped[str] = mapped_column(String(255))
    shopify_access_token_encrypted: Mapped[str] = mapped_column(String(1024))
    shopify_webhook_secret: Mapped[str] = mapped_column(String(255))
    shopify_location_id: Mapped[str] = mapped_column(String(120), default="")
    bims_posale_id: Mapped[int] = mapped_column(Integer, default=0)
    bims_warehouse_id: Mapped[int] = mapped_column(Integer, default=0)
    bims_warehouse_ids: Mapped[list] = mapped_column(JSON, default=list)
    bims_company_id: Mapped[int] = mapped_column(Integer, default=0)
    bims_currency_id: Mapped[int] = mapped_column(Integer, default=0)
    bims_payment_method_id: Mapped[int] = mapped_column(Integer, default=0)
    default_customer_contact_id: Mapped[int] = mapped_column(Integer, default=0)
    reorder_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    reorder_strategy: Mapped[str] = mapped_column(String(40), default="none")
    bims_timezone: Mapped[str] = mapped_column(String(64), default="America/Asuncion")
    payment_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_config: Mapped[dict] = mapped_column(JSON, default=dict)
    field_mappings: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    push_orders_to_bims: Mapped[bool] = mapped_column(Boolean, default=True)
    portal_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auto_import_products: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_import_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_import_interval_minutes: Mapped[int] = mapped_column(Integer, default=360)
    auto_import_only_with_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_mode: Mapped[str] = mapped_column(String(16), default="full", server_default="full")
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    #: Push worker cadence (the PULL cadence is ``sync_interval_minutes``).
    push_interval_minutes: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    #: The push worker declines to push from a snapshot older than this.
    snapshot_max_age_minutes: Mapped[int] = mapped_column(
        Integer, default=120, server_default="120"
    )
    #: A pull with MORE than this fraction fewer rows than the previous snapshot is rejected.
    pull_max_drop_ratio: Mapped[float] = mapped_column(Float, default=0.30, server_default="0.3")


class SyncStateModel(Base):
    __tablename__ = "sync_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True, unique=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    product_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Recent completed-run durations: [{"at", "seconds", "mode"}, ...], oldest first.
    sync_duration_history: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    #: Outcome of the latest pull tick (ok | rejected | skipped | failed) with its numbers.
    last_pull_outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Outcome of the latest push tick (ok | unchanged | declined | needs_confirmation | failed).
    last_push_outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class OAuthStateModel(Base):
    __tablename__ = "oauth_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop: Mapped[str] = mapped_column(String(255), index=True)
    state: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RekeyReportModel(Base):
    __tablename__ = "rekey_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class RekeyResolutionModel(Base):
    """A merchant-portal decision applied to one variant from a rekey report.

    ``(report_id, variant_id)`` is unique so a resolution is idempotent per
    report: re-posting the same decision is detected and returned as
    "already_resolved" rather than re-applied (see the portal API).
    """

    __tablename__ = "rekey_resolutions"
    __table_args__ = (UniqueConstraint("report_id", "variant_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id"), index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("rekey_reports.id"), index=True)
    variant_id: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)


class AuditLogModel(Base):
    """Append-only audit trail. See alembic/versions/0009_audit_logs.py."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_tenant_id_created_at", "tenant_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=True
    )
    actor: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(120))
    entity: Mapped[str] = mapped_column(String(120))
    entity_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)


class PortalUserModel(Base):
    """A named per-user login for the merchant self-service portal.

    See alembic/versions/0010_portal_users.py. Coexists with the older
    shared ``tenants.portal_token_hash`` access key; either credential is
    accepted by the portal API (see ``api/portal.py``).
    """

    __tablename__ = "portal_users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_portal_users_tenant_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentIntentModel(Base):
    """A hosted-checkout payment link created for a Shopify order.

    Persisted so (a) the Pagopar callback — which never echoes the
    merchant's own order id, only its internal ``hash_pedido`` — can
    resolve which Shopify order to mark paid, and (b) the merchant portal
    can list recent payment links (``GET /api/portal/{slug}/payments``) so
    staff can copy one into an order confirmation email.
    """

    __tablename__ = "payment_intents"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", "order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    order_id: Mapped[str] = mapped_column(String(255))
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(10), default="PYG")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    checkout_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc
    )


class ProcessedEventModel(Base):
    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("tenant_id", "source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(60))
    external_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), default="processed")
    payload_hash: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)


class OpsJobModel(Base):
    """A background-run catalog/rekey ops command triggered via the admin API.

    ``status`` transitions: queued -> running -> (succeeded|failed), or
    running -> interrupted if the process restarts mid-job (see
    ``JobRunner.mark_interrupted_on_startup``).
    """

    __tablename__ = "ops_jobs"
    __table_args__ = (Index("ix_ops_jobs_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id"))
    command: Mapped[str] = mapped_column(String(40))
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CatalogSnapshotModel(Base):
    """Metadata of one BIMS pull. See alembic/versions/0018_catalog_snapshot.py.

    A pull is written as a ``building`` generation that readers cannot see;
    ``promote`` flips ``is_current`` in one short transaction. At most one row
    per tenant is current (partial unique index).
    """

    __tablename__ = "catalog_snapshots"
    __table_args__ = (
        Index(
            "uq_catalog_snapshots_current",
            "tenant_id",
            unique=True,
            sqlite_where=text("is_current"),
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_pulled: Mapped[int] = mapped_column(Integer, default=0)
    stock_rows: Mapped[int] = mapped_column(Integer, default=0)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    reported_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stop_reason: Mapped[str] = mapped_column(String(32), default="")
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    #: Per-phase durations in seconds.
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Catalog stats computed by the pull (BIMS eligible, with stock, ...).
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Why a pull was rejected or failed.
    rejection: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class CatalogSnapshotProductModel(Base):
    __tablename__ = "catalog_snapshot_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalog_snapshots.id", ondelete="CASCADE"), index=True
    )
    bims_id: Mapped[str] = mapped_column(String(64))
    #: SKU matched against Shopify (``code2`` unless the tenant maps another field).
    sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    code2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    sell_price: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exclude_ecommerce: Mapped[bool] = mapped_column(Boolean, default=False)
    company_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CatalogSnapshotStockModel(Base):
    __tablename__ = "catalog_snapshot_stock"
    __table_args__ = (UniqueConstraint("snapshot_id", "sku"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalog_snapshots.id", ondelete="CASCADE"), index=True
    )
    sku: Mapped[str] = mapped_column(String(255))
    #: Aggregated across the tenant's warehouses (what is pushed).
    quantity: Mapped[float] = mapped_column(Float)
    #: {"<warehouse_id>": quantity}; the diagnostic detail behind ``quantity``.
    by_warehouse: Mapped[dict] = mapped_column(JSON, default=dict)
