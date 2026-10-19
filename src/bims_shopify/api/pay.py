"""Buyer-facing vPOS pages: the signed iframe checkout, plus the return and cancel pages.

The routes are unauthenticated by design (the buyer is anonymous); every page is
gated by an HMAC-signed, expiring token. The Bancard ``process_id`` rides inside
the checkout token, so it cannot be forged and needs no lookup. Pages never
change payment state: only the verified callback path does.
"""
from __future__ import annotations

import html
import json
import secrets

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.checkout_link import CheckoutClaims, CheckoutTokenError, derive_link_secret, verify_token
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import VposConfigurationError, vpos_base_url
from bims_shopify.domain.tenant import PaymentProvider, Tenant
from bims_shopify.logging import get_logger

from .deps import get_db_session, get_settings, get_tenant_repository

logger = get_logger(__name__)

router = APIRouter(prefix="/pay/bancard_vpos", tags=["pay"])

SDK_PATH = "/checkout/javascript/dist/bancard-checkout-3.0.0.js"
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _page(status: int, title: str, body: str, *, head: str = "", csp: str | None = None) -> HTMLResponse:
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>{head}</head>"
        f'<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>'
    )
    headers = dict(_HEADERS)
    headers["Content-Security-Policy"] = csp or "default-src 'none'; style-src 'unsafe-inline'"
    return HTMLResponse(document, status_code=status, headers=headers)


def _message(status: int, title: str, text: str) -> HTMLResponse:
    return _page(status, title, f"<p>{html.escape(text)}</p>")


def _invalid(error: CheckoutTokenError | None = None) -> HTMLResponse:
    status = 410 if error is not None and error.expired else 403
    return _message(status, "Link not valid", "This payment link is invalid or has expired.")


def _js_string(value: str) -> str:
    """A JS string literal that is safe inside an inline ``<script>`` element."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(chr(0x2028), "\\u2028")
        .replace(chr(0x2029), "\\u2029")
    )


async def _resolve(
    slug: str,
    token: str,
    repo: SqlAlchemyTenantRepository,
    settings: Settings,
    *,
    require_process_id: bool,
) -> tuple[Tenant, CheckoutClaims] | HTMLResponse:
    """Verify the token before touching anything else; failures reveal nothing."""
    secret = derive_link_secret(settings)
    if not secret:
        return _message(503, "Unavailable", "Payments are temporarily unavailable.")
    try:
        claims = verify_token(secret, token, slug, require_process_id=require_process_id)
    except CheckoutTokenError as exc:
        return _invalid(exc)
    tenant = await repo.get_by_slug(slug)
    if tenant is None or not tenant.active or tenant.payment_provider != PaymentProvider.BANCARD_VPOS:
        return _invalid()
    return tenant, claims


@router.get("/{slug}/return/{token}")
async def return_page(
    slug: str,
    token: str,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    settings: Settings = Depends(get_settings),
):
    resolved = await _resolve(slug, token, repo, settings, require_process_id=False)
    if isinstance(resolved, HTMLResponse):
        return resolved
    return _message(
        200,
        "Thank you",
        "We are confirming your payment with the bank. Your order will update shortly.",
    )


@router.get("/{slug}/cancel/{token}")
async def cancel_page(
    slug: str,
    token: str,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    settings: Settings = Depends(get_settings),
):
    resolved = await _resolve(slug, token, repo, settings, require_process_id=False)
    if isinstance(resolved, HTMLResponse):
        return resolved
    return _message(200, "Payment cancelled", "Your payment was cancelled and you were not charged.")


@router.get("/{slug}/{token}")
async def checkout_page(
    slug: str,
    token: str,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    resolved = await _resolve(slug, token, repo, settings, require_process_id=True)
    if isinstance(resolved, HTMLResponse):
        return resolved
    tenant, claims = resolved
    try:
        origin = vpos_base_url((tenant.provider_config or {}).get("environment"))
    except VposConfigurationError:
        logger.error("vpos_pay_page_bad_environment", tenant=slug)
        return _message(503, "Unavailable", "Payments are temporarily unavailable.")
    intent = await SqlAlchemyPaymentIntentRepository(session).get_by_order_id(
        tenant.id, "bancard_vpos", claims.order_id
    )
    if intent is not None and intent.status == "paid":
        return _message(200, "Already paid", "This order has already been paid.")

    nonce = secrets.token_urlsafe(16)
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}' {origin}; frame-src {origin}; "
        f"connect-src {origin}; img-src {origin} data:; style-src 'unsafe-inline' {origin}"
    )
    script = (
        f'<script nonce="{nonce}">window.addEventListener("load", function () {{ '
        f'Bancard.Checkout.createForm("iframe-container", {_js_string(claims.process_id or "")}, {{}}); }});'
        "</script>"
    )
    body = (
        '<div id="iframe-container" aria-live="polite"></div>'
        f'<script src="{html.escape(origin + SDK_PATH)}" nonce="{nonce}"></script>{script}'
    )
    return _page(200, "Complete your payment", body, csp=csp)
