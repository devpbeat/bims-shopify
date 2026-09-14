"""CRUD router for tenants, protected by a static admin bearer token."""
from __future__ import annotations

import hashlib
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

from .deps import get_tenant_repository, require_admin

router = APIRouter(prefix="/tenants", tags=["tenants"], dependencies=[Depends(require_admin)])


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

    @field_validator("bims_timezone")
    @classmethod
    def _validate_bims_timezone(cls, value: str) -> str:
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
        )


@router.get("", response_model=list[TenantOut])
async def list_tenants(repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository)):
    tenants = await repo.list_all()
    return [TenantOut.from_domain(tenant) for tenant in tenants]


@router.post("", response_model=TenantOut, status_code=201)
async def create_tenant(
    body: TenantCreate, repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository)
):
    tenant = Tenant(id=None, **body.model_dump())
    created = await repo.create(tenant)
    return TenantOut.from_domain(created)


@router.get("/{slug}", response_model=TenantOut)
async def get_tenant(slug: str, repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository)):
    tenant = await repo.get_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantOut.from_domain(tenant)


@router.put("/{slug}", response_model=TenantOut)
async def update_tenant(
    slug: str, body: TenantCreate, repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository)
):
    existing = await repo.get_by_slug(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    updated = Tenant(id=existing.id, **body.model_dump())
    saved = await repo.update(updated)
    return TenantOut.from_domain(saved)


@router.delete("/{slug}", status_code=204)
async def delete_tenant(slug: str, repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository)):
    existing = await repo.get_by_slug(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    await repo.delete(existing.id)


class PortalTokenOut(BaseModel):
    portal_token: str


@router.post("/{slug}/portal-token", response_model=PortalTokenOut)
async def create_portal_token(
    slug: str, repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository)
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
    return PortalTokenOut(portal_token=token)
