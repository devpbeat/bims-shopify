"""Hosted-checkout payment routes: create checkout link and receive provider callback."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.payments.bancard_via_bims_provider import (
    BancardViaBIMSProvider,
)
from bims_shopify.adapters.payments.pagopar_provider import PagoparProvider
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.application.payments import CreatePaymentLink, HandlePaymentCallback
from bims_shopify.domain.tenant import PaymentProvider, Tenant
from bims_shopify.logging import get_logger

from .deps import get_db_session, get_tenant_repository

logger = get_logger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


def _build_provider(provider_name: str, tenant: Tenant):
    if provider_name == PaymentProvider.PAGOPAR.value:
        return PagoparProvider()
    if provider_name == PaymentProvider.BANCARD.value:
        return BancardViaBIMSProvider(BIMSClient(tenant))
    raise HTTPException(status_code=404, detail=f"Unknown payment provider: {provider_name}")


@router.post("/{provider}/{tenant_slug}/checkout")
async def create_checkout(
    provider: str,
    tenant_slug: str,
    request: Request,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = Depends(get_db_session),
):
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    body = await request.json()
    order_id = str(body.get("order_id"))
    provider_impl = _build_provider(provider, tenant)
    intents = SqlAlchemyPaymentIntentRepository(session)
    audit = SqlAlchemyAuditLogger(session)
    use_case = CreatePaymentLink(provider_impl, intents)
    intent = await use_case.run(
        tenant, order_id, float(body.get("amount", 0)), body.get("currency", "PYG")
    )
    await audit.log(
        actor="portal",
        action="payment.link_created",
        entity="order",
        tenant_id=tenant.id,
        entity_id=order_id,
        payload={"provider": provider, "status": intent.status},
    )
    return {"checkout_url": intent.checkout_url, "provider_reference": intent.provider_reference}


async def _extract_callback_payload(request: Request) -> dict:
    """Extract a callback payload regardless of provider transport shape.

    Pagopar POSTs a JSON body (`resultado`/`hash_pedido`). Bancard, proxied
    through BIMS' `bims_pay/gw_callback` webhook, may instead hit this route
    as a GET with query params (BIMS defines that endpoint as `GET`). Form
    POSTs are also accepted for completeness.
    """
    if request.method == "GET":
        return dict(request.query_params)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        return dict(form)
    body = await request.body()
    if not body:
        return dict(request.query_params)
    return await request.json()


async def _handle_payment_callback(
    provider: str,
    tenant_slug: str,
    request: Request,
    repo: SqlAlchemyTenantRepository,
    session: AsyncSession,
):
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    payload = await _extract_callback_payload(request)
    provider_impl = _build_provider(provider, tenant)
    storefront = ShopifyClient(tenant)
    processed_events = SqlAlchemySyncStateRepository(session)
    intents = SqlAlchemyPaymentIntentRepository(session)
    audit = SqlAlchemyAuditLogger(session)
    use_case = HandlePaymentCallback(provider_impl, storefront, processed_events, intents)

    await audit.log(
        actor=provider,
        action="payment.callback_received",
        entity="order",
        tenant_id=tenant.id,
    )
    try:
        intent = await use_case.run(tenant, payload)
    except Exception as exc:
        logger.warning("payment_callback_verification_failed", provider=provider, tenant=tenant_slug)
        await audit.log(
            actor=provider,
            action="payment.verification_failed",
            entity="order",
            tenant_id=tenant.id,
            payload={"error": type(exc).__name__},
        )
        raise

    await audit.log(
        actor=provider,
        action="payment.confirmed" if intent.status == "paid" else "payment.verification_failed",
        entity="order",
        tenant_id=tenant.id,
        entity_id=intent.order_id or None,
        payload={"status": intent.status},
    )
    return {"status": intent.status}


@router.post("/{provider}/{tenant_slug}/callback")
async def payment_callback(
    provider: str,
    tenant_slug: str,
    request: Request,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = Depends(get_db_session),
):
    return await _handle_payment_callback(provider, tenant_slug, request, repo, session)


@router.get("/{provider}/{tenant_slug}/callback")
async def payment_callback_get(
    provider: str,
    tenant_slug: str,
    request: Request,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = Depends(get_db_session),
):
    return await _handle_payment_callback(provider, tenant_slug, request, repo, session)
