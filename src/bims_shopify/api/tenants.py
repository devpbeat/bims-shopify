"""CRUD router for tenants, protected by a static admin bearer token."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import replace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.portal_user_repository import (
    SqlAlchemyPortalUserRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.api.portal_auth import hash_password
from bims_shopify.application.sync_interval import (
    PULL_MODE,
    PUSH_MODE,
    RECONCILE_MODE,
    interval_floor_for,
    rejection_detail,
)
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, SyncMode, Tenant

from .deps import (
    get_audit_logger,
    get_db_session,
    get_portal_user_repository,
    get_tenant_repository,
    require_admin,
)

#: Upper bound for the per-tenant sync interval (one day).
MAX_SYNC_INTERVAL_MINUTES = 1440
#: Upper bound for the reconcile interval (one week).
MAX_RECONCILE_INTERVAL_MINUTES = 10080
#: Upper bound for the snapshot staleness limit (one week).
MAX_SNAPSHOT_MAX_AGE_MINUTES = 10080

router = APIRouter(prefix="/tenants", tags=["tenants"], dependencies=[Depends(require_admin)])


async def _enforce_interval_floor(
    session: AsyncSession, tenant: Tenant, before: Tenant | None = None
) -> None:
    """Reject (422) a worker interval below its measured minimum safe interval.

    * PULL (``sync_interval_minutes``): floor from recent completed pulls. Every
      pull is a full pull, so the floor never depends on ``sync_mode``.
    * PUSH (``push_interval_minutes``): floor from recent completed pushes.
    * RECONCILE (``reconcile_interval_minutes``): floor from recent completed
      reconciles, never below an hour.

    With no history the pull gets the documented conservative default and the
    push the 1-minute minimum. Each is checked only when it is being changed
    (``before`` given), so an unrelated edit is never blocked by a
    pre-existing low value.
    """
    repo = SqlAlchemySyncStateRepository(session)
    if before is None or tenant.sync_interval_minutes != before.sync_interval_minutes:
        floor = await interval_floor_for(repo, tenant.id, PULL_MODE)
        if tenant.sync_interval_minutes < floor.minutes:
            raise HTTPException(
                status_code=422, detail=rejection_detail(floor, tenant.sync_interval_minutes)
            )
    if before is None or tenant.push_interval_minutes != before.push_interval_minutes:
        floor = await interval_floor_for(repo, tenant.id, PUSH_MODE)
        if tenant.push_interval_minutes < floor.minutes:
            raise HTTPException(
                status_code=422,
                detail=rejection_detail(floor, tenant.push_interval_minutes, "push_interval_minutes"),
            )
    if before is None or tenant.reconcile_interval_minutes != before.reconcile_interval_minutes:
        floor = await interval_floor_for(repo, tenant.id, RECONCILE_MODE)
        if tenant.reconcile_interval_minutes < floor.minutes:
            raise HTTPException(
                status_code=422,
                detail=rejection_detail(
                    floor, tenant.reconcile_interval_minutes, "reconcile_interval_minutes"
                ),
            )


#: The snapshot may be this many pull intervals old before the push declines:
#: two means exactly one pull can fail, be rejected or be skipped without the
#: push going dark; a third consecutive miss does stop it.
STALENESS_PULL_INTERVALS = 2


def _enforce_staleness_limit(before: Tenant | None, tenant: Tenant) -> None:
    """Reject (422) a staleness limit the pull cadence can never satisfy.

    Checked only when the pull interval or the limit changes. With a limit
    shorter than the pull cadence the snapshot is "stale" for part of every
    cycle and the push would decline for no reason.
    """
    if (
        before is not None
        and before.sync_interval_minutes == tenant.sync_interval_minutes
        and before.snapshot_max_age_minutes == tenant.snapshot_max_age_minutes
    ):
        return
    needed = STALENESS_PULL_INTERVALS * tenant.sync_interval_minutes
    if tenant.snapshot_max_age_minutes < needed:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"snapshot_max_age_minutes={tenant.snapshot_max_age_minutes} is too small for "
                    f"sync_interval_minutes={tenant.sync_interval_minutes}: the push would decline "
                    f"as stale during part of every cycle. It must be at least "
                    f"{STALENESS_PULL_INTERVALS} x the pull interval = {needed} minutes, so one "
                    "failed or rejected pull does not stop the push."
                ),
                "field": "snapshot_max_age_minutes",
                "min_minutes": needed,
                "requested_minutes": tenant.snapshot_max_age_minutes,
            },
        )


def _reschedule_live(request: Request, tenant: Tenant) -> None:
    """Apply the saved tenant's interval to the running scheduler, so a
    change takes effect immediately instead of at the next restart."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.reschedule_tenant(tenant)


class TenantCreate(BaseModel):
    slug: str
    bims_base_url: str
    bims_api_key: str
    shopify_shop_domain: str
    shopify_access_token: str
    shopify_webhook_secret: str
    shopify_location_id: str = ""
    bims_posale_id: int = 0
    bims_warehouse_id: int = 0
    bims_warehouse_ids: list[int] = []
    bims_company_id: int = 0
    bims_currency_id: int = 0
    bims_payment_method_id: int = 0
    default_customer_contact_id: int = 0
    reorder_threshold: float = 0.0
    reorder_strategy: ReorderStrategy = ReorderStrategy.NONE
    bims_timezone: str = "America/Asuncion"
    payment_provider: PaymentProvider | None = None
    provider_config: dict = {}
    field_mappings: dict = {}
    active: bool = True
    push_orders_to_bims: bool = True
    auto_import_products: bool = False
    auto_import_publish: bool = False
    auto_import_interval_minutes: int = 360
    auto_import_only_with_stock: bool = True
    sync_mode: SyncMode = SyncMode.FULL
    sync_interval_minutes: int = Field(default=30, ge=1, le=MAX_SYNC_INTERVAL_MINUTES)
    push_interval_minutes: int = Field(default=5, ge=1, le=MAX_SYNC_INTERVAL_MINUTES)
    reconcile_interval_minutes: int = Field(default=480, ge=1, le=MAX_RECONCILE_INTERVAL_MINUTES)
    snapshot_max_age_minutes: int = Field(default=120, ge=1, le=MAX_SNAPSHOT_MAX_AGE_MINUTES)
    pull_max_drop_ratio: float = Field(default=0.30, gt=0, le=1)
    auto_create_missing: bool = True
    auto_publish: bool = True

    @field_validator("bims_timezone")
    @classmethod
    def _validate_bims_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value!r}") from exc
        return value


class TenantPatch(BaseModel):
    """Partial update payload: every field is optional and omitted fields
    are left untouched, including secrets (bims_api_key,
    shopify_access_token, shopify_webhook_secret)."""

    slug: str | None = None
    bims_base_url: str | None = None
    bims_api_key: str | None = None
    shopify_shop_domain: str | None = None
    shopify_access_token: str | None = None
    shopify_webhook_secret: str | None = None
    shopify_location_id: str | None = None
    bims_posale_id: int | None = None
    bims_warehouse_id: int | None = None
    bims_warehouse_ids: list[int] | None = None
    bims_company_id: int | None = None
    bims_currency_id: int | None = None
    bims_payment_method_id: int | None = None
    default_customer_contact_id: int | None = None
    reorder_threshold: float | None = None
    reorder_strategy: ReorderStrategy | None = None
    bims_timezone: str | None = None
    payment_provider: PaymentProvider | None = None
    provider_config: dict | None = None
    field_mappings: dict | None = None
    active: bool | None = None
    push_orders_to_bims: bool | None = None
    auto_import_products: bool | None = None
    auto_import_publish: bool | None = None
    auto_import_interval_minutes: int | None = None
    auto_import_only_with_stock: bool | None = None
    sync_mode: SyncMode | None = None
    sync_interval_minutes: int | None = Field(default=None, ge=1, le=MAX_SYNC_INTERVAL_MINUTES)
    push_interval_minutes: int | None = Field(default=None, ge=1, le=MAX_SYNC_INTERVAL_MINUTES)
    reconcile_interval_minutes: int | None = Field(
        default=None, ge=1, le=MAX_RECONCILE_INTERVAL_MINUTES
    )
    snapshot_max_age_minutes: int | None = Field(
        default=None, ge=1, le=MAX_SNAPSHOT_MAX_AGE_MINUTES
    )
    pull_max_drop_ratio: float | None = Field(default=None, gt=0, le=1)
    auto_create_missing: bool | None = None
    auto_publish: bool | None = None

    @field_validator("bims_api_key", "shopify_access_token", "shopify_webhook_secret")
    @classmethod
    def _reject_empty_secret(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("secret fields cannot be set to an empty string")
        return value

    @field_validator("bims_timezone")
    @classmethod
    def _validate_bims_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value!r}") from exc
        return value


class TenantOut(BaseModel):
    id: int
    slug: str
    bims_base_url: str
    shopify_shop_domain: str
    shopify_location_id: str
    bims_warehouse_id: int
    bims_warehouse_ids: list[int]
    reorder_threshold: float
    reorder_strategy: ReorderStrategy
    bims_timezone: str
    payment_provider: PaymentProvider | None
    active: bool
    push_orders_to_bims: bool
    auto_import_products: bool
    auto_import_publish: bool
    auto_import_interval_minutes: int
    auto_import_only_with_stock: bool
    sync_mode: SyncMode
    sync_interval_minutes: int
    push_interval_minutes: int
    reconcile_interval_minutes: int
    snapshot_max_age_minutes: int
    pull_max_drop_ratio: float
    auto_create_missing: bool
    auto_publish: bool

    @classmethod
    def from_domain(cls, tenant: Tenant) -> TenantOut:
        return cls(
            id=tenant.id,
            slug=tenant.slug,
            bims_base_url=tenant.bims_base_url,
            shopify_shop_domain=tenant.shopify_shop_domain,
            shopify_location_id=tenant.shopify_location_id,
            bims_warehouse_id=tenant.bims_warehouse_id,
            bims_warehouse_ids=list(tenant.bims_warehouse_ids or []),
            reorder_threshold=tenant.reorder_threshold,
            reorder_strategy=tenant.reorder_strategy,
            bims_timezone=tenant.bims_timezone,
            payment_provider=tenant.payment_provider,
            active=tenant.active,
            push_orders_to_bims=tenant.push_orders_to_bims,
            auto_import_products=tenant.auto_import_products,
            auto_import_publish=tenant.auto_import_publish,
            auto_import_interval_minutes=tenant.auto_import_interval_minutes,
            auto_import_only_with_stock=tenant.auto_import_only_with_stock,
            sync_mode=tenant.sync_mode,
            sync_interval_minutes=tenant.sync_interval_minutes,
            push_interval_minutes=tenant.push_interval_minutes,
            reconcile_interval_minutes=tenant.reconcile_interval_minutes,
            snapshot_max_age_minutes=tenant.snapshot_max_age_minutes,
            pull_max_drop_ratio=tenant.pull_max_drop_ratio,
            auto_create_missing=tenant.auto_create_missing,
            auto_publish=tenant.auto_publish,
        )


@router.get("", response_model=list[TenantOut])
async def list_tenants(repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository)):
    tenants = await repo.list_all()
    return [TenantOut.from_domain(tenant) for tenant in tenants]


@router.post("", response_model=TenantOut, status_code=201)
async def create_tenant(
    request: Request,
    body: TenantCreate,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
    session: AsyncSession = Depends(get_db_session),
):
    tenant = Tenant(id=None, **body.model_dump())
    await _enforce_interval_floor(session, tenant)
    _enforce_staleness_limit(None, tenant)
    created = await repo.create(tenant)
    await audit.log(
        actor="admin",
        action="tenant.create",
        entity="tenant",
        tenant_id=created.id,
        entity_id=created.slug,
        payload={"slug": created.slug},
    )
    _reschedule_live(request, created)
    return TenantOut.from_domain(created)


@router.get("/{slug}", response_model=TenantOut)
async def get_tenant(slug: str, repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository)):
    tenant = await repo.get_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantOut.from_domain(tenant)


@router.put("/{slug}", response_model=TenantOut)
async def update_tenant(
    request: Request,
    slug: str,
    body: TenantCreate,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
    session: AsyncSession = Depends(get_db_session),
):
    existing = await repo.get_by_slug(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    updated = Tenant(id=existing.id, **body.model_dump())
    await _enforce_interval_floor(session, updated, existing)
    _enforce_staleness_limit(existing, updated)
    saved = await repo.update(updated)
    await audit.log(
        actor="admin",
        action="tenant.update",
        entity="tenant",
        tenant_id=saved.id,
        entity_id=saved.slug,
        payload={"slug": saved.slug},
    )
    _reschedule_live(request, saved)
    return TenantOut.from_domain(saved)


@router.patch("/{slug}", response_model=TenantOut)
async def patch_tenant(
    request: Request,
    slug: str,
    body: TenantPatch,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
    session: AsyncSession = Depends(get_db_session),
):
    """Partially update a tenant: only fields present in the payload are
    changed. Omitted fields — especially secrets (bims_api_key,
    shopify_access_token, shopify_webhook_secret) — are left untouched, so
    callers never need to resend secrets they aren't rotating.
    """
    existing = await repo.get_by_slug(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    changes = body.model_dump(exclude_unset=True)
    updated = replace(existing, **changes)
    await _enforce_interval_floor(session, updated, existing)
    _enforce_staleness_limit(existing, updated)
    saved = await repo.update(updated)
    await audit.log(
        actor="admin",
        action="tenant.update",
        entity="tenant",
        tenant_id=saved.id,
        entity_id=saved.slug,
        # Field NAMES only, never values, so secrets never land in the audit log.
        payload={"fields": sorted(changes.keys())},
    )
    _reschedule_live(request, saved)
    return TenantOut.from_domain(saved)


@router.delete("/{slug}", status_code=204)
async def delete_tenant(
    request: Request,
    slug: str,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
):
    existing = await repo.get_by_slug(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    # Logged before the delete (not after): audit_logs.tenant_id is a real FK
    # to tenants.id, so inserting it once the tenant row is gone would fail.
    await audit.log(
        actor="admin",
        action="tenant.delete",
        entity="tenant",
        tenant_id=existing.id,
        entity_id=existing.slug,
        payload={"slug": existing.slug},
    )
    await repo.delete(existing.id)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.remove_tenant(existing.id)


class PortalTokenOut(BaseModel):
    portal_token: str


@router.post("/{slug}/portal-token", response_model=PortalTokenOut)
async def create_portal_token(
    slug: str,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
):
    """Mint a new merchant-portal bearer token for this tenant.

    Only the SHA-256 hash is persisted; the plaintext token is returned
    exactly once and cannot be recovered afterwards. Minting a new token
    invalidates any previously issued one (it overwrites the stored hash).
    """
    tenant = await repo.get_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    await repo.set_portal_token_hash(tenant.id, token_hash)
    # Never log the token itself, only that a mint happened.
    await audit.log(
        actor="admin",
        action="tenant.portal_token_mint",
        entity="tenant",
        tenant_id=tenant.id,
        entity_id=tenant.slug,
    )
    return PortalTokenOut(portal_token=token)


class PortalUserIn(BaseModel):
    email: str
    password: str
    name: str | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized:
            raise ValueError("email must be a valid address")
        return normalized

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("password must be at least 8 characters")
        return value


class PortalUserOut(BaseModel):
    id: int
    email: str
    name: str | None
    active: bool


@router.post("/{slug}/portal-users", response_model=PortalUserOut, status_code=201)
async def create_portal_user(
    slug: str,
    body: PortalUserIn,
    tenant_repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    user_repo: SqlAlchemyPortalUserRepository = Depends(get_portal_user_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
):
    """Create or update (upsert by email) a named per-user portal login.

    Upserting rather than erroring on an existing email lets an admin reset
    a forgotten password by re-posting the same email with a new one. The
    password is bcrypt-hashed before it ever touches the repository/DB —
    the plaintext is never persisted or logged, only that a
    create/update happened (see the audit entry below).
    """
    tenant = await tenant_repo.get_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    password_hash = hash_password(body.password)
    user = await user_repo.upsert(
        tenant_id=tenant.id,
        email=body.email,
        password_hash=password_hash,
        name=body.name,
    )
    await audit.log(
        actor="admin",
        action="tenant.portal_user_upsert",
        entity="portal_user",
        tenant_id=tenant.id,
        entity_id=body.email,
    )
    return PortalUserOut(id=user.id, email=user.email, name=user.name, active=user.active)
