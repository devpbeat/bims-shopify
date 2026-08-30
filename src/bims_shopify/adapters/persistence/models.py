"""SQLAlchemy ORM models."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow_naive() -> datetime:
    """Timezone-aware "now", stripped to naive UTC for storage.

    These plain DateTime columns are naive (no `timezone=True`), and SQLite
    round-trips any stored datetime as naive regardless. Building the value
    via `datetime.now(UTC)` avoids the deprecated `datetime.utcnow()` while
    the stored value stays naive-UTC, matching what these columns already
    held.
    """
    return datetime.now(UTC).replace(tzinfo=None)


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
    bims_company_id: Mapped[int] = mapped_column(Integer, default=0)
    bims_currency_id: Mapped[int] = mapped_column(Integer, default=0)
    bims_payment_method_id: Mapped[int] = mapped_column(Integer, default=0)
    default_customer_contact_id: Mapped[int] = mapped_column(Integer, default=0)
    reorder_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    reorder_strategy: Mapped[str] = mapped_column(String(40), default="none")
    payment_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_config: Mapped[dict] = mapped_column(JSON, default=dict)
    field_mappings: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SyncStateModel(Base):
    __tablename__ = "sync_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True, unique=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    product_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_summary: Mapped[dict] = mapped_column(JSON, default=dict)


class OAuthStateModel(Base):
    __tablename__ = "oauth_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop: Mapped[str] = mapped_column(String(255), index=True)
    state: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ProcessedEventModel(Base):
    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("tenant_id", "source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(60))
    external_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), default="processed")
    payload_hash: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive)
