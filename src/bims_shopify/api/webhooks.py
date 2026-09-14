"""Shopify webhook receiver: verifies HMAC, returns fast, processes in background."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
    hash_payload,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.adapters.shopify.webhook_auth import verify_shopify_hmac
from bims_shopify.application.process_shopify_order import ProcessShopifyOrder
from bims_shopify.config import Settings
from bims_shopify.domain.sale import SaleLineItem, SaleOrder, SalePayment
from bims_shopify.logging import get_logger

from .deps import get_settings, get_tenant_repository

router = APIRouter(prefix="/webhooks/shopify", tags=["webhooks"])
logger = get_logger(__name__)


def build_sale_from_webhook_payload(tenant, payload: dict) -> SaleOrder:
    """Translate a Shopify order webhook payload into a BIMS SaleOrder.

    PYG (Paraguayan Guarani) has no fractional subunit, but Shopify always
    formats money fields with two decimals (e.g. "150000.00") regardless of
    the store's currency, and some price-list/rounding setups can leave a
    genuine fractional remainder (e.g. "150000.50"). BIMS's Sale/SaleProduct
    schemas accept floats, but posting fractional Guaranies produces
    malformed/rejected ledger entries downstream, so every money amount is
    rounded to the nearest whole Guarani here, at the boundary, before it
    ever reaches the BIMS payload builder.
    """
    order_id = str(payload.get("id", ""))
    amount = round(float(payload.get("total_price", 0) or 0))
    return SaleOrder(
        external_id=order_id,
        contact_id=tenant.default_customer_contact_id,
        posale_id=tenant.bims_posale_id,
        agency_id=tenant.bims_warehouse_id,
        company_id=tenant.bims_company_id,
        currency_id=tenant.bims_currency_id,
        amount=amount,
        line_items=[
            SaleLineItem(
                product_id=int(line.get("product_id", 0) or 0),
                quantity=float(line.get("quantity", 0) or 0),
                price=round(float(line.get("price", 0) or 0)),
            )
            for line in payload.get("line_items", [])
        ],
        payments=[
            SalePayment(
                payment_method_id=tenant.bims_payment_method_id,
                amount=amount,
            )
        ],
    )


@router.post("/app")
async def receive_app_level_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_shopify_hmac_sha256: str = Header(default=""),
    x_shopify_topic: str = Header(default=""),
    x_shopify_shop_domain: str = Header(default=""),
    settings: Settings = Depends(get_settings),
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
) -> dict[str, str]:
    """Receive app-level webhooks declared in shopify.app.toml (orders/paid,
    orders/cancelled). Unlike the per-tenant `/webhooks/shopify/{slug}` route,
    Shopify signs these with the app's single client secret (SHOPIFY_API_SECRET)
    rather than a per-tenant secret, and identifies the shop via the
    `X-Shopify-Shop-Domain` header instead of a path segment.
    """
    body = await request.body()
    if not verify_shopify_hmac(settings.shopify_api_secret, body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    if not x_shopify_shop_domain:
        raise HTTPException(status_code=400, detail="Missing X-Shopify-Shop-Domain header")

    tenant = await repo.get_by_slug(x_shopify_shop_domain.split(".")[0])
    if tenant is None:
        # Shopify still expects a 200 for webhooks addressed to shops we
        # don't recognize (e.g. a stale subscription after uninstall);
        # returning an error would cause Shopify to retry indefinitely.
        logger.warning("app_webhook_unknown_shop", shop=x_shopify_shop_domain, topic=x_shopify_topic)
        return {"status": "ignored"}

    payload = await request.json()
    background_tasks.add_task(
        _process_order_webhook,
        request.app.state.session_factory,
        settings,
        tenant.slug,
        x_shopify_topic,
        payload,
        body,
    )
    return {"status": "accepted"}


@router.post("/compliance/{topic:path}")
async def receive_compliance_webhook(
    topic: str,
    request: Request,
    x_shopify_hmac_sha256: str = Header(default=""),
    x_shopify_topic: str = Header(default=""),
    settings: Settings = Depends(get_settings),
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
) -> dict[str, str]:
    """GDPR mandatory compliance webhooks: customers/data_request,
    customers/redact, shop/redact. Must always verify HMAC and respond 200.

    `topic` accepts the path form (e.g. `/compliance/shop/redact`, one uri
    per topic in shopify.app.toml) as well as a single shared uri
    (`/compliance`) that relies on the `X-Shopify-Topic` header instead.
    """
    body = await request.body()
    if not verify_shopify_hmac(settings.shopify_api_secret, body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    resolved_topic = topic or x_shopify_topic
    payload = await request.json()

    if resolved_topic == "shop/redact":
        shop_domain = payload.get("shop_domain", "")
        slug = shop_domain.split(".")[0] if shop_domain else ""
        tenant = await repo.get_by_slug(slug) if slug else None
        if tenant is not None:
            await repo.delete(tenant.id)
        logger.info("shopify_compliance_shop_redact", shop=shop_domain)
    else:
        # customers/data_request and customers/redact: this app does not
        # store Shopify customer PII (only order totals/line items needed
        # to post a BIMS sale), so there is nothing further to erase/export.
        logger.info("shopify_compliance_webhook", topic=resolved_topic, payload_keys=list(payload.keys()))

    return {"status": "ok"}


@router.post("/{tenant_slug}")
async def receive_shopify_webhook(
    tenant_slug: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_shopify_hmac_sha256: str = Header(default=""),
    x_shopify_topic: str = Header(default=""),
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    body = await request.body()
    if not verify_shopify_hmac(tenant.shopify_webhook_secret, body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    payload = await request.json()
    background_tasks.add_task(
        _process_order_webhook,
        request.app.state.session_factory,
        settings,
        tenant_slug,
        x_shopify_topic,
        payload,
        body,
    )
    return {"status": "accepted"}


async def _process_order_webhook(
    session_factory: async_sessionmaker,
    settings: Settings,
    tenant_slug: str,
    topic: str,
    payload: dict,
    raw_body: bytes,
) -> None:
    """Background job: dedupe via processed_events, then push a Sale to BIMS.

    Reuses the app's shared engine/session_factory (set on app.state at
    startup) instead of creating a brand-new engine per webhook delivery,
    which would leak a connection pool on every call.

    `settings` is threaded through explicitly from the request (rather than
    re-read via `config.get_settings()`, which re-parses `.env`/the process
    environment) so the `fernet_key` used to decrypt tenant secrets always
    matches the one the request-scoped `Settings` object — and therefore the
    one tenant secrets were encrypted with — instead of silently diverging
    whenever a caller constructs `Settings` directly (e.g. tests, or any
    future non-default configuration source).
    """
    from bims_shopify.adapters.persistence.crypto import SecretBox

    async with session_factory() as session:
        tenant_repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        sync_state_repo = SqlAlchemySyncStateRepository(session)

        tenant = await tenant_repo.get_by_slug(tenant_slug)
        if tenant is None or not tenant.active:
            return

        order_id = str(payload.get("id", ""))
        if not order_id:
            return

        # Atomically claim this (tenant, source, external_id) via the DB
        # unique constraint. This closes the race where two concurrent
        # deliveries of the same webhook both pass a SELECT-then-INSERT
        # check before either has committed, which would otherwise both
        # proceed to call BIMS.
        claimed = await sync_state_repo.try_claim_event(
            tenant.id, "shopify_webhook", order_id, hash_payload(raw_body)
        )
        if not claimed:
            return

        if not tenant.push_orders_to_bims:
            # BIMS is the source of truth for this tenant: staff invoice
            # manually in BIMS, and the periodic sync feeds Shopify
            # inventory from BIMS. Do NOT create a Sale or run reorder
            # logic here; still record the event for idempotency/audit.
            logger.info(
                "order_push_disabled",
                tenant=tenant.slug,
                order_id=order_id,
            )
            await sync_state_repo.mark_event_status(tenant.id, "shopify_webhook", order_id, "processed")
            return

        sale = build_sale_from_webhook_payload(tenant, payload)

        client = BIMSClient(tenant)
        try:
            try:
                erp = BIMSERPAdapter(client)
                use_case = ProcessShopifyOrder(erp)
                await use_case.run(tenant, sale)
            except Exception:
                # Release the claim so a future retry/redelivery of this
                # webhook can attempt processing again instead of being
                # silently swallowed as "already processed".
                await sync_state_repo.delete_event(tenant.id, "shopify_webhook", order_id)
                raise
        finally:
            await client.aclose()

        await sync_state_repo.mark_event_status(tenant.id, "shopify_webhook", order_id, "processed")
