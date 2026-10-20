"""Endpoint for the Thank you page checkout UI extension.

The extension runs in a Web Worker on an origin Shopify may change, so CORS is
open to any origin (the documented requirement). That makes the session token
the only gate: it is verified before anything is read, and the tenant comes
from the shop domain it names, matched against the stored domain.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.application.buyer_pay_link import FindBuyerPayLink
from bims_shopify.config import Settings
from bims_shopify.logging import get_logger
from bims_shopify.shopify_session_token import SessionTokenError, verify_session_token

from .deps import get_db_session, get_settings, get_tenant_repository
from .portal_auth import LoginRateLimiter

logger = get_logger(__name__)

router = APIRouter(prefix="/checkout-extension", tags=["checkout-extension"])

RATE_LIMIT_MAX_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60.0
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "600",
}
_HEADERS = {**_CORS, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _reply(status: int, body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=_HEADERS)


def _limiter(request: Request) -> LoginRateLimiter:
    limiter = getattr(request.app.state, "checkout_extension_limiter", None)
    if limiter is None:
        limiter = LoginRateLimiter(
            max_attempts=RATE_LIMIT_MAX_REQUESTS, window_seconds=RATE_LIMIT_WINDOW_SECONDS
        )
        request.app.state.checkout_extension_limiter = limiter
    return limiter


@router.options("/pay-link")
async def pay_link_preflight() -> Response:
    return Response(status_code=204, headers=_CORS)


@router.post("/pay-link")
async def pay_link(
    request: Request,
    tenants: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Return the Bancard pay URL for an order, only while a payment is outstanding."""
    limiter = _limiter(request)
    client_host = request.client.host if request.client else "unknown"
    if not limiter.check(f"ip:{client_host}"):
        return _reply(429, {"error": "rate_limited"})

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return _reply(401, {"error": "unauthorized"})
    if not settings.shopify_api_secret or not settings.shopify_api_key:
        logger.error("checkout_extension_not_configured")
        return _reply(503, {"error": "unavailable"})
    try:
        claims = verify_session_token(
            token.strip(),
            client_secret=settings.shopify_api_secret,
            client_id=settings.shopify_api_key,
        )
    except SessionTokenError as exc:
        logger.info("checkout_extension_token_rejected", reason=str(exc))
        return _reply(401, {"error": "unauthorized"})
    if not limiter.check(f"shop:{claims.shop_domain}"):
        return _reply(429, {"error": "rate_limited"})

    try:
        body = await request.json()
    except ValueError:
        body = None
    order_ref = body.get("order_id") if isinstance(body, dict) else None

    lookup = FindBuyerPayLink(tenants, SqlAlchemyPaymentIntentRepository(session))
    result = await lookup.run(claims.shop_domain, order_ref)
    if result.pay_url is None:
        return _reply(200, {"status": "none"})
    return _reply(200, {"status": "pay", "pay_url": result.pay_url})
