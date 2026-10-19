"""Merchant portal payments configuration (Bancard vPOS credentials).

The private key is write-only: it is accepted on PUT, never returned, and an
omitted or blank value leaves the stored key untouched. Authentication reuses
``get_portal_tenant`` (admin token, legacy token or session JWT) so a tenant
can only ever reach its own slug.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api.portal import get_portal_tenant
from bims_shopify.api.tenants import _validate_vpos_config
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, Tenant

from .deps import get_audit_logger, get_settings, get_tenant_repository

router = APIRouter(prefix="/api/portal/{slug}/payments-config", tags=["portal"])

_CONFIG_FIELDS = ("public_key", "environment", "gateway_name")


class PaymentsConfigOut(BaseModel):
    provider: str | None
    editable: bool
    public_key: str | None
    environment: str | None
    gateway_name: str | None
    private_key_configured: bool
    confirm_url: str


class PaymentsConfigIn(BaseModel):
    provider: PaymentProvider = PaymentProvider.BANCARD_VPOS
    public_key: str | None = None
    environment: str | None = None
    gateway_name: str | None = None
    private_key: str | None = None


class PaymentsTestOut(BaseModel):
    ok: bool
    reason: str
    message: str


def _editable(tenant: Tenant) -> bool:
    return tenant.payment_provider in (None, PaymentProvider.BANCARD_VPOS)


def _to_out(tenant: Tenant, settings: Settings) -> PaymentsConfigOut:
    config = tenant.provider_config or {}
    base = settings.public_base_url.rstrip("/")
    return PaymentsConfigOut(
        provider=tenant.payment_provider.value if tenant.payment_provider else None,
        editable=_editable(tenant),
        public_key=config.get("public_key"),
        environment=config.get("environment"),
        gateway_name=config.get("gateway_name"),
        private_key_configured=bool(tenant.bancard_vpos_private_key),
        confirm_url=f"{base}/payments/bancard_vpos/{tenant.slug}/callback",
    )


@router.get("", response_model=PaymentsConfigOut)
async def get_payments_config(
    tenant: Tenant = Depends(get_portal_tenant),
    settings: Settings = Depends(get_settings),
) -> PaymentsConfigOut:
    return _to_out(tenant, settings)


@router.put("", response_model=PaymentsConfigOut)
async def put_payments_config(
    body: PaymentsConfigIn,
    tenant: Tenant = Depends(get_portal_tenant),
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
    settings: Settings = Depends(get_settings),
) -> PaymentsConfigOut:
    if body.provider is not PaymentProvider.BANCARD_VPOS:
        raise HTTPException(status_code=422, detail="provider must be bancard_vpos")
    if not _editable(tenant):
        raise HTTPException(
            status_code=409, detail="This store uses another payment provider"
        )
    config: dict[str, Any] = dict(tenant.provider_config or {})
    changed: list[str] = []
    for name in _CONFIG_FIELDS:
        value = getattr(body, name)
        if value is not None and value.strip() and config.get(name) != value.strip():
            config[name] = value.strip()
            changed.append(name)
    new_key = body.private_key if body.private_key and body.private_key.strip() else None
    tenant.payment_provider = PaymentProvider.BANCARD_VPOS
    tenant.provider_config = config
    _validate_vpos_config(tenant)
    if new_key is not None:
        tenant.bancard_vpos_private_key = new_key
        changed.append("private_key")
    if not tenant.bancard_vpos_private_key:
        raise HTTPException(status_code=422, detail="A private key is required for vPOS")
    await repo.update(tenant)
    if changed:
        await audit.log(
            actor="portal",
            action="payments.config_updated",
            entity="tenant",
            tenant_id=tenant.id,
            entity_id=tenant.slug,
            payload={"fields": changed},
        )
    return _to_out(tenant, settings)
