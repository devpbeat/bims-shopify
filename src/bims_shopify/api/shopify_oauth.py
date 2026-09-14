"""Shopify OAuth install flow: GET /shopify/install and GET /shopify/callback.

On a successful callback, the merchant's tenant is matched to a
pre-provisioned BIMS-side tenant (see `_match_tenant`), linked to the real
Shopify shop domain, and — if it already carries working BIMS credentials —
auto-activated after one cheap live validation call. Auto-activation also
kicks off an immediate dry-run sync so the operator can review the first
`needs_confirmation` report before any real push happens.
"""
from __future__ import annotations

import asyncio
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.bims.client import BIMSAPIError, BIMSClient
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.oauth_state_repository import (
    SqlAlchemyOAuthStateRepository,
)
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
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

from .deps import get_audit_logger, get_db_session, get_settings, get_tenant_repository

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

def _lock_for_tenant(request: Request, tenant_id: int) -> asyncio.Lock:
    """Reuse the scheduler's per-tenant lock (same one used by the manual and
    scheduled sync runs) so a post-activation dry-run kickoff can never race
    with any other sync for the same tenant."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        return scheduler.lock_for_tenant(tenant_id)
    locks: dict[int, asyncio.Lock] = request.app.state.__dict__.setdefault(
        "_manual_sync_locks", {}
    )
    if tenant_id not in locks:
        locks[tenant_id] = asyncio.Lock()
    return locks[tenant_id]

async def _match_tenant(
    tenant_repo: SqlAlchemyTenantRepository, shop: str
) -> tuple[Tenant, bool]:
    """Find the tenant this install belongs to, in priority order:

    1. exact `shopify_shop_domain == shop` match (already linked before);
    2. `slug == <shop subdomain>` match (pre-provisioned by us, not yet
       linked to a real Shopify domain — e.g. seeded with a guessed domain);
    3. otherwise create a brand-new, inactive tenant (current behavior).

    Returns (tenant, created).
    """
    slug = shop.split(".")[0]

    tenant = await tenant_repo.get_by_shopify_domain(shop)
    if tenant is not None:
        return tenant, False

    tenant = await tenant_repo.get_by_slug(slug)
    if tenant is not None:
        return tenant, False

    tenant = Tenant(
        id=None,
        slug=slug,
        bims_base_url="",
        bims_api_key="",
        shopify_shop_domain=shop,
        shopify_access_token="",
        shopify_webhook_secret="",
        shopify_location_id="",
        bims_posale_id=0,
        bims_warehouse_id=0,
        bims_company_id=0,
        bims_currency_id=0,
        bims_payment_method_id=0,
        default_customer_contact_id=0,
        active=False,
    )
    return tenant, True

async def _validate_bims_credentials(tenant: Tenant) -> bool:
    """One cheap live read against BIMS to confirm the pre-provisioned
    credentials actually work before we ever flip a tenant to active."""
    client = BIMSClient(tenant)
    try:
        await client.get("/api/currencies/index.json")
        return True
    except BIMSAPIError as exc:
        logger.info(
            "bims_validation_failed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            error=str(exc),
        )
        return False
    except Exception as exc:  # network/connection errors: treat as invalid
        logger.info(
            "bims_validation_failed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            error=str(exc),
        )
        return False
    finally:
        await client.aclose()

async def _run_post_activation_dry_run(session_factory, lock: asyncio.Lock, tenant: Tenant) -> None:
    from bims_shopify.api.sync import _do_run_sync

    async with lock, session_factory() as session:
        sync_state_repo = SqlAlchemySyncStateRepository(session)
        try:
            result = await _do_run_sync(tenant, dry_run=True, session=session)
        except Exception as exc:  # must never crash the background task
            logger.error(
                "post_activation_dry_run_failed",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                error=str(exc),
            )
            return
        summary = {"dry_run": True, "trigger": "post_activation", **result}
        await sync_state_repo.set_last_run_summary(tenant.id, summary)
        logger.info(
            "post_activation_dry_run_completed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
        )

def _schedule_post_activation_dry_run(request: Request, tenant: Tenant) -> None:
    if tenant.id is None:
        return
    lock = _lock_for_tenant(request, tenant.id)
    if lock.locked():
        logger.info(
            "post_activation_dry_run_skipped_lock_held",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
        )
        return
    session_factory = request.app.state.session_factory
    task = asyncio.create_task(_run_post_activation_dry_run(session_factory, lock, tenant))
    tasks: set[asyncio.Task] = request.app.state.__dict__.setdefault(
        "_oauth_background_tasks", set()
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)

def _render_landing_page(tenant: Tenant, activated_now: bool) -> str:
    status = "active" if tenant.active else "pending BIMS configuration"
    if tenant.active:
        next_step = (
            "Your store is now syncing with BIMS automatically. "
            "A first dry-run review is available to the operator before any live push."
            if activated_now
            else "Your store is syncing with BIMS."
        )
    else:
        next_step = (
            "Installation succeeded, but we could not validate your BIMS configuration yet. "
            "Contact support to finish setup — no action is needed on Shopify."
        )
    slug = escape(tenant.slug)
    status_text = escape(status)
    next_step_text = escape(next_step)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Installation complete</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 560px; margin: 80px auto; padding: 0 24px; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.5rem; }}
  .status {{ font-weight: 600; }}
  p {{ line-height: 1.5; }}
</style>
</head>
<body>
<h1>Installation succeeded</h1>
<p>Tenant: <strong>{slug}</strong></p>
<p>Status: <span class="status">{status_text}</span></p>
<p>{next_step_text}</p>
</body>
</html>"""

@router.get("/callback")
async def callback(
    request: Request,
    shop: str = Query(...),
    code: str = Query(...),
    state: str = Query(...),
    settings: Settings = Depends(get_settings),
    state_repo: SqlAlchemyOAuthStateRepository = Depends(get_oauth_state_repository),
    tenant_repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
) -> HTMLResponse:
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

    tenant, created = await _match_tenant(tenant_repo, shop)

    # Update Shopify-side fields only; every BIMS field on `tenant` is left
    # untouched so a pre-provisioned tenant keeps its BIMS configuration.
    tenant.shopify_shop_domain = shop
    tenant.shopify_access_token = access_token
    tenant.shopify_location_id = location_id or tenant.shopify_location_id

    if created:
        tenant = await tenant_repo.create(tenant)
    else:
        tenant = await tenant_repo.update(tenant)

    logger.info("shopify_app_installed", shop=shop, slug=tenant.slug, created=created)
    await audit.log(
        actor="system",
        action="oauth.install_linked",
        entity="tenant",
        tenant_id=tenant.id,
        entity_id=tenant.slug,
        payload={"shop": shop, "created": created},
    )

    activated_now = False
    has_bims_config = bool(tenant.bims_base_url) and bool(tenant.bims_api_key)
    if not tenant.active and has_bims_config:
        if await _validate_bims_credentials(tenant):
            tenant.active = True
            tenant = await tenant_repo.update(tenant)
            activated_now = True
            logger.info(
                "tenant_auto_activated", tenant_slug=tenant.slug, tenant_id=tenant.id
            )
            await audit.log(
                actor="system",
                action="oauth.install_activated",
                entity="tenant",
                tenant_id=tenant.id,
                entity_id=tenant.slug,
            )
        else:
            logger.info(
                "tenant_auto_activation_skipped",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                reason="bims_validation_failed",
            )
    elif not tenant.active:
        logger.info(
            "tenant_auto_activation_skipped",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            reason="missing_bims_config",
        )

    if activated_now:
        _schedule_post_activation_dry_run(request, tenant)

    return HTMLResponse(_render_landing_page(tenant, activated_now))
