"""Buyer-facing vPOS pages: the signed iframe checkout, plus the return and cancel pages.

The routes are unauthenticated by design (the buyer is anonymous); every page is
gated by an HMAC-signed, expiring token that names the order. The payment page
reads the live Shopify total and mints (or reuses) the Bancard operation on open.
"""
from __future__ import annotations

import html
import json
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.payments.factory import build_provider
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.application.payments import CreatePaymentLink, OrderAmountError, resolve_live_order
from bims_shopify.checkout_link import CheckoutClaims, CheckoutTokenError, derive_link_secret, verify_token
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import (
    VposAttemptsExhaustedError,
    VposConfigurationError,
    VposError,
    vpos_base_url,
)
from bims_shopify.domain.payment import PaymentStatus
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
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    """Where Bancard sends the buyer back.

    The browser return proves nothing, so the outcome is read out of band with
    ``get_confirmation`` before anything else happens. Marking the Shopify order
    paid stays with the server-to-server callback.
    """
    resolved = await _resolve(slug, token, repo, settings, require_process_id=False)
    if isinstance(resolved, HTMLResponse):
        return resolved
    tenant, claims = resolved
    intents = SqlAlchemyPaymentIntentRepository(session)
    intent = await intents.get_by_order_id(tenant.id, "bancard_vpos", claims.order_id)
    if intent is None or not intent.provider_reference:
        return _still_confirming()
    if intent.status == "paid":
        return await _confirmed(tenant, claims.order_id)

    audit = SqlAlchemyAuditLogger(session)
    provider = build_provider("bancard_vpos", tenant, settings=settings, audit=audit)
    try:
        verdict = await provider.verify_payment(tenant, intent)
    except Exception as exc:
        # Credential problems and outages are ours, never a failed payment for the buyer.
        await _log_page_error(audit, tenant, claims.order_id, type(exc).__name__)
        return _still_confirming()
    if verdict == PaymentStatus.CONFIRMED:
        return await _confirmed(tenant, claims.order_id)
    if verdict == PaymentStatus.FAILED:
        if intent.status == "pending":
            await intents.update_status(tenant.id, "bancard_vpos", claims.order_id, "failed")
        retry = f"/pay/bancard_vpos/{slug}/{token}"
        return _page(
            200,
            "Payment declined",
            "<p>Your payment was not approved and you were not charged.</p>"
            f'<p><a href="{html.escape(retry, quote=True)}">Try again</a></p>',
        )
    return _still_confirming()


def _still_confirming() -> HTMLResponse:
    return _message(
        200,
        "Thank you",
        "We are still confirming your payment with the bank. Your order will update shortly.",
    )


async def _confirmed(tenant: Tenant, order_id: str) -> Response:
    """Send a verified buyer to the Shopify order status page, else confirm in-app."""
    status_url: str | None = None
    try:
        status_url = (await resolve_live_order(tenant, order_id, ShopifyClient(tenant))).status_url
    except Exception as exc:
        logger.warning("vpos_return_status_url_failed", tenant=tenant.slug, error=type(exc).__name__)
    if status_url and urlsplit(status_url).scheme == "https":
        return RedirectResponse(status_url, status_code=303, headers=_HEADERS)
    return _message(200, "Payment received", "Thank you. Your payment was received and your order will update shortly.")


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
    """The single buyer door: the token names the order, the live total decides the charge."""
    resolved = await _resolve(slug, token, repo, settings, require_process_id=False)
    if isinstance(resolved, HTMLResponse):
        return resolved
    tenant, claims = resolved
    try:
        origin = vpos_base_url((tenant.provider_config or {}).get("environment"))
    except VposConfigurationError:
        logger.error("vpos_pay_page_bad_environment", tenant=slug)
        return _message(503, "Unavailable", "Payments are temporarily unavailable.")
    intents = SqlAlchemyPaymentIntentRepository(session)
    stored = await intents.get_by_order_id(tenant.id, "bancard_vpos", claims.order_id)
    if stored is not None and stored.status == "paid":
        return _already_paid()

    try:
        live = await resolve_live_order(tenant, claims.order_id, ShopifyClient(tenant))
    except OrderAmountError as exc:
        if exc.not_found:
            return _message(404, "Order not found", "We could not find this order.")
        return _message(422, "Cannot pay online", "This order cannot be paid online. Please contact the store.")
    except Exception as exc:
        logger.warning("vpos_pay_page_order_lookup_failed", tenant=slug, error=type(exc).__name__)
        return _message(503, "Unavailable", "We could not load your order. Please try again shortly.")
    if live.paid:
        return _already_paid()

    audit = SqlAlchemyAuditLogger(session)
    provider = build_provider("bancard_vpos", tenant, settings=settings, audit=audit)
    try:
        intent = await CreatePaymentLink(provider, intents, audit).run(
            tenant, claims.order_id, live.amount, "PYG"
        )
    except VposAttemptsExhaustedError:
        return await _page_error(
            audit, tenant, claims.order_id, "attempts_exhausted", 409,
            "Too many payment attempts. Please contact the store.",
        )
    except VposConfigurationError:
        return await _page_error(
            audit, tenant, claims.order_id, "credentials", 503,
            "Payments are temporarily unavailable. Please contact the store.",
        )
    except VposError:
        return await _page_error(
            audit, tenant, claims.order_id, "bancard_unavailable", 502,
            "We could not reach the payment service. Please try again in a few minutes.",
        )
    if not intent.process_id:
        return await _page_error(
            audit, tenant, claims.order_id, "missing_process_id", 502,
            "We could not start your payment. Please try again in a few minutes.",
        )

    nonce = secrets.token_urlsafe(16)
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}' {origin}; frame-src {origin}; "
        f"connect-src {origin}; img-src {origin} data:; style-src 'unsafe-inline' {origin}"
    )
    script = (
        f'<script nonce="{nonce}">window.addEventListener("load", function () {{ '
        f'Bancard.Checkout.createForm("iframe-container", {_js_string(intent.process_id)}, {{}}); }});'
        "</script>"
    )
    body = (
        '<div id="iframe-container" aria-live="polite"></div>'
        f'<script src="{html.escape(origin + SDK_PATH)}" nonce="{nonce}"></script>{script}'
    )
    return _page(200, "Complete your payment", body, csp=csp)


def _already_paid() -> HTMLResponse:
    return _message(200, "Already paid", "This order has already been paid.")


async def _page_error(
    audit: SqlAlchemyAuditLogger, tenant: Tenant, order_id: str, reason: str, status: int, text: str
) -> HTMLResponse:
    """Log and audit a failed page open; the buyer sees plain copy, never the cause."""
    await _log_page_error(audit, tenant, order_id, reason)
    return _message(status, "Payment unavailable", text)


async def _log_page_error(audit: SqlAlchemyAuditLogger, tenant: Tenant, order_id: str, reason: str) -> None:
    logger.error("vpos_pay_page_failed", tenant=tenant.slug, order_id=order_id, reason=reason)
    await audit.log(
        actor="system",
        action="payment.page_error",
        entity="order",
        tenant_id=tenant.id,
        entity_id=order_id,
        payload={"reason": reason},
    )
