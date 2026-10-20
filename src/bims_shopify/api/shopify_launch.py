"""Single sign-on from the Shopify admin into the merchant portal.

``shopify.app.toml`` points ``application_url`` at this service's root with
``embedded = false``, so opening the app in the Shopify admin sends the operator
to ``GET /`` with Shopify-signed query parameters. A verified request proves
Shopify is vouching for staff of that shop, so we mint the *same* portal
session JWT the email/password login issues (``portal_auth.create_session_token``)
and land the operator in their tenant's portal.

The portal session is a bearer JWT held in the SPA's ``sessionStorage`` (not a
cookie), so the hand-off is a tiny same-origin page that stores the token under
the SPA's storage key and replaces itself with the portal URL. The token never
appears in a URL, a redirect ``Location``, or a log line.

Every failure (bad signature, stale timestamp, malformed shop, unknown or
inactive tenant) returns the same redirect to the normal portal login, so the
response never reveals whether a shop exists.
"""
from __future__ import annotations

import json
import secrets
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.adapters.shopify.launch import (
    is_fresh_timestamp,
    is_valid_launch_shop,
    normalize_shop,
    verify_launch_hmac,
)
from bims_shopify.api.portal_auth import (
    SESSION_TOKEN_TTL,
    create_session_token,
    get_portal_session_secret,
)
from bims_shopify.config import Settings
from bims_shopify.logging import get_logger

from .deps import get_audit_logger, get_settings, get_tenant_repository

router = APIRouter(tags=["shopify-launch"])
logger = get_logger(__name__)

PORTAL_LOGIN_PATH = "/portal"
# Must match `TOKEN_PREFIX` in frontend/src/session.ts.
SESSION_STORAGE_PREFIX = "bims-portal-token:"
# SSO has no portal-user row; 0 marks "authenticated by Shopify" in the claim.
SSO_USER_ID = 0


def _reject(reason: str) -> Response:
    # Reason is logged for operators only; the client always sees the same redirect.
    logger.warning("shopify_launch_rejected", reason=reason)
    return RedirectResponse(PORTAL_LOGIN_PATH, status_code=303)


def _js_string(value: str) -> str:
    """JSON-encode for safe embedding inside an inline ``<script>``."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _handoff_page(*, slug: str, token: str) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    target = f"/portal/{quote(slug, safe='')}"
    body = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>BIMS</title></head><body>"
        "<p>Signing you in&hellip; / Iniciando sesi&oacute;n&hellip;</p>"
        f"<script nonce=\"{nonce}\">\n"
        f"const token = {_js_string(token)};\n"
        f"const storageKey = {_js_string(SESSION_STORAGE_PREFIX + slug)};\n"
        f"const target = {_js_string(target)};\n"
        "sessionStorage.setItem(storageKey, token);\n"
        "location.replace(target);\n"
        "</script></body></html>"
    )
    return HTMLResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )


@router.get("/", include_in_schema=False)
async def shopify_app_launch(
    request: Request,
    tenant_repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
    settings: Settings = Depends(get_settings),
) -> Response:
    query_items = list(request.query_params.multi_items())
    params = dict(query_items)

    if not verify_launch_hmac(query_items, settings.shopify_api_secret):
        return _reject("invalid_signature")
    if not is_fresh_timestamp(params.get("timestamp", "")):
        return _reject("stale_timestamp")

    # Past this point the request is authentic, so `shop` is trustworthy input
    # for auditing; pre-verification failures are never written to the audit log.
    shop = normalize_shop(params.get("shop", ""))
    if not is_valid_launch_shop(shop):
        return _reject("invalid_shop")

    # Resolve by the stored domain, never by inferring a slug from the subdomain.
    tenant = await tenant_repo.get_by_shopify_domain(shop)
    if tenant is None or tenant.id is None:
        await audit.log(
            actor="shopify",
            action="portal.sso_failed",
            entity="portal_sso",
            entity_id=shop,
            payload={"reason": "unknown_shop"},
        )
        return _reject("unknown_shop")
    if not tenant.active:
        await audit.log(
            actor="shopify",
            action="portal.sso_failed",
            entity="portal_sso",
            tenant_id=tenant.id,
            entity_id=shop,
            payload={"reason": "inactive_tenant"},
        )
        return _reject("inactive_tenant")

    token = create_session_token(
        secret=get_portal_session_secret(settings), tenant_id=tenant.id, user_id=SSO_USER_ID
    )
    logger.info("shopify_launch_sso_success", tenant_slug=tenant.slug)
    await audit.log(
        actor="shopify",
        action="portal.sso_login",
        entity="portal_sso",
        tenant_id=tenant.id,
        entity_id=shop,
        payload={"outcome": "success", "ttl_seconds": int(SESSION_TOKEN_TTL / timedelta(seconds=1))},
    )
    return _handoff_page(slug=tenant.slug, token=token)
