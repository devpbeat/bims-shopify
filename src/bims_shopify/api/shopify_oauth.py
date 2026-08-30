"""Shopify OAuth install flow: GET /shopify/install and GET /shopify/callback."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.oauth_state_repository import (
    SqlAlchemyOAuthStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.adapters.shopify.oauth import (
    DEFAULT_SCOPES,
    ShopifyOAuthClient,
    build_authorize_url,
    is_valid_shop_domain,
    verify_callback_hmac,
)
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger

from .deps import get_db_session, get_settings, get_tenant_repository

router = APIRouter(prefix="/shopify", tags=["shopify-oauth"])
logger = get_logger(__name__)


async def get_oauth_state_repository(
    session: AsyncSession = Depends(get_db_session),
) -> SqlAlchemyOAuthStateRepository:
    return SqlAlchemyOAuthStateRepository(session)


@router.get("/install")
async def install(
    shop: str = Query(...),
    settings: Settings = Depends(get_settings),
    state_repo: SqlAlchemyOAuthStateRepository = Depends(get_oauth_state_repository),
) -> RedirectResponse:
    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    state = await state_repo.create(shop)
    redirect_uri = f"{settings.public_base_url}/shopify/callback"
    authorize_url = build_authorize_url(
        shop=shop,
        client_id=settings.shopify_api_key,
        redirect_uri=redirect_uri,
        state=state,
        scopes=DEFAULT_SCOPES,
    )
    return RedirectResponse(authorize_url, status_code=302)


@router.get("/callback")
async def callback(
    request: Request,
    shop: str = Query(...),
    code: str = Query(...),
    state: str = Query(...),
    settings: Settings = Depends(get_settings),
    state_repo: SqlAlchemyOAuthStateRepository = Depends(get_oauth_state_repository),
    tenant_repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
) -> dict[str, str]:
    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    if not verify_callback_hmac(dict(request.query_params), settings.shopify_api_secret):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    if not await state_repo.consume(shop, state):
        raise HTTPException(status_code=401, detail="Invalid or expired state")

    oauth_client = ShopifyOAuthClient()
    try:
        token_payload = await oauth_client.exchange_code_for_token(
            shop=shop,
            client_id=settings.shopify_api_key,
            client_secret=settings.shopify_api_secret,
            code=code,
        )
        access_token = token_payload["access_token"]
        location_id = await oauth_client.fetch_primary_location_id(
            shop=shop, access_token=access_token
        )
    finally:
        await oauth_client.aclose()

    slug = shop.split(".")[0]
    existing = await tenant_repo.get_by_slug(slug)

    if existing is None:
        tenant = Tenant(
            id=None,
            slug=slug,
            bims_base_url="",
            bims_api_key="",
            shopify_shop_domain=shop,
            shopify_access_token=access_token,
            shopify_webhook_secret="",
            shopify_location_id=location_id,
            bims_posale_id=0,
            bims_warehouse_id=0,
            bims_company_id=0,
            bims_currency_id=0,
            bims_payment_method_id=0,
            default_customer_contact_id=0,
            active=False,
        )
        await tenant_repo.create(tenant)
    else:
        existing.shopify_shop_domain = shop
        existing.shopify_access_token = access_token
        existing.shopify_location_id = location_id or existing.shopify_location_id
        await tenant_repo.update(existing)

    logger.info("shopify_app_installed", shop=shop, slug=slug)
    return {"status": "installed", "shop": shop}
