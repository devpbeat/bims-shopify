"""Merchant self-service portal API.

Authenticated per-tenant with a bearer token (minted by an admin via
``POST /tenants/{slug}/portal-token``, see ``api/tenants.py``) rather than
the static admin token — this is a distinct, narrower-scoped credential so a
store's staff can review and resolve their own ``rekey_reports`` findings
without holding the platform admin token. The token is verified with
``hmac.compare_digest`` against the SHA-256 hash stored on the tenant to
avoid both timing attacks and ever persisting the plaintext token.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.audit_repository import (
    DEFAULT_AUDIT_LIMIT,
    MAX_AUDIT_LIMIT,
    SqlAlchemyAuditLogger,
)
from bims_shopify.adapters.persistence.portal_user_repository import (
    SqlAlchemyPortalUserRepository,
)
from bims_shopify.adapters.persistence.rekey_repository import SqlAlchemyRekeyRepository
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.adapters.shopify.client import ShopifyClient, ShopifyGraphQLError
from bims_shopify.api.portal_auth import (
    LoginRateLimiter,
    SessionTokenError,
    create_session_token,
    decode_session_token,
    get_portal_session_secret,
    verify_password,
)
from bims_shopify.config import Settings
from bims_shopify.datetime_utils import ensure_aware_utc
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger

from .deps import (
    get_audit_logger,
    get_db_session,
    get_login_rate_limiter,
    get_portal_user_repository,
    get_settings,
    get_tenant_repository,
)

router = APIRouter(prefix="/api/portal/{slug}", tags=["portal"])
logger = get_logger(__name__)

VALID_ACTIONS = {"keep", "delete", "approve_sku", "ignore"}
_REPORT_CATEGORIES = ("planned_rewrites", "name_mismatch", "unresolved", "duplicate_target")

_APPLY_ERRORS: tuple[type[Exception], ...] = (
    ShopifyGraphQLError,
    httpx.TransportError,
    TimeoutError,
)


async def get_portal_tenant(
    slug: str,
    authorization: str = Header(default=""),
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    settings: Settings = Depends(get_settings),
) -> Tenant:
    """Resolve and authenticate the tenant addressed by the ``{slug}`` path segment.

    Looking the tenant up by slug (rather than by reversing the token) means
    a valid credential for tenant A can never be accepted against tenant
    B's slug — there is no cross-tenant lookup path, only same-tenant
    comparisons.

    Three credential shapes are accepted on the same ``Authorization: Bearer``
    header, checked in order:

    1. The static platform admin token (``settings.admin_token``), compared
       with ``hmac.compare_digest``. This is a superuser credential: it
       authorizes the request for *any* tenant slug, the same way it already
       authorizes every ``/ops/*`` endpoint via ``require_admin``. This exists
       because the operator SPA logs the operator in with the single admin
       token and then calls these same portal data endpoints — without this
       path every portal page load 401s for operators.
    2. The legacy shared portal access token, hashed and compared against
       ``tenants.portal_token_hash`` with ``hmac.compare_digest``.
    3. A per-user session JWT minted by ``POST /api/portal/{slug}/login``,
       verified for signature, expiry, and that its ``tenant_id`` claim
       matches *this* tenant (a valid session for store A can't be replayed
       against store B's slug even though JWTs aren't tenant-scoped by URL).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    presented = authorization.removeprefix("Bearer ").strip()
    if not presented:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if hmac.compare_digest(presented, settings.admin_token):
        tenant = await repo.get_by_slug(slug)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Not found")
        return tenant

    tenant = await repo.get_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    stored_hash = await repo.get_portal_token_hash(tenant.id)
    if stored_hash:
        presented_hash = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        if hmac.compare_digest(presented_hash, stored_hash):
            return tenant

    secret = get_portal_session_secret(settings)
    try:
        claims = decode_session_token(secret=secret, token=presented)
    except SessionTokenError:
        raise HTTPException(status_code=401, detail="Unauthorized") from None

    if claims.get("tenant_id") != tenant.id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    return tenant


class PortalLoginIn(BaseModel):
    email: str
    password: str


class PortalLoginOut(BaseModel):
    token: str
    expires_in: int


@router.post("/login", response_model=PortalLoginOut)
async def portal_login(
    slug: str,
    body: PortalLoginIn,
    tenant_repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    user_repo: SqlAlchemyPortalUserRepository = Depends(get_portal_user_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
    settings: Settings = Depends(get_settings),
    limiter: LoginRateLimiter = Depends(get_login_rate_limiter),
):
    """Email + password login for the merchant portal.

    Rate-limited per ``{slug}+{email}`` (5/min) to blunt naive
    credential-stuffing without needing a shared cache. Never logs the
    password — only success/failure and the email are recorded, in both
    structured logs and the audit trail.
    """
    email = body.email.strip().lower()
    rate_key = f"{slug}:{email}"
    if not limiter.check(rate_key):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again shortly.")

    tenant = await tenant_repo.get_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    user = await user_repo.get_by_tenant_and_email(tenant.id, email)
    if (
        user is None
        or not user.active
        or user.tenant_id != tenant.id
        or not verify_password(body.password, user.password_hash)
    ):
        logger.warning("portal_login_failed", tenant_slug=slug, email=email)
        await audit.log(
            actor="portal",
            action="portal.login_failed",
            entity="portal_user",
            tenant_id=tenant.id,
            entity_id=email,
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")

    await user_repo.record_login(user.id)
    logger.info("portal_login_success", tenant_slug=slug, email=email, user_id=user.id)
    await audit.log(
        actor="portal",
        action="portal.login_success",
        entity="portal_user",
        tenant_id=tenant.id,
        entity_id=email,
    )

    secret = get_portal_session_secret(settings)
    token = create_session_token(secret=secret, tenant_id=tenant.id, user_id=user.id)
    return PortalLoginOut(token=token, expires_in=int(timedelta(hours=24).total_seconds()))


def _index_report_variants(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten every category of a rekey report payload into one lookup by variant_id.

    Entries from ``unresolved`` do not carry a ``product_id`` (see
    ``ops/rekey_skus.py``), so callers that need it (e.g. ``delete``) must
    check for its presence rather than assume it exists.
    """
    index: dict[str, dict[str, Any]] = {}
    for category in _REPORT_CATEGORIES:
        for entry in payload.get(category) or []:
            variant_id = entry.get("variant_id")
            if variant_id is not None:
                index[str(variant_id)] = entry
    return index


class ResolutionIn(BaseModel):
    variant_id: str
    action: Literal["keep", "delete", "approve_sku", "ignore"]
    note: str | None = None


class ResolutionsIn(BaseModel):
    resolutions: list[ResolutionIn]


class ResolutionResultOut(BaseModel):
    variant_id: str
    action: str
    status: str
    error: str | None = None


@router.get("/payments")
async def list_payments(
    tenant: Tenant = Depends(get_portal_tenant),
    session: AsyncSession = Depends(get_db_session),
):
    """Recent hosted-checkout payment links for this tenant.

    Lets merchant staff copy a Pagopar link into an order confirmation
    email (sending it automatically is a later iteration).
    """
    from bims_shopify.adapters.persistence.payment_intent_repository import (
        SqlAlchemyPaymentIntentRepository,
    )

    repo = SqlAlchemyPaymentIntentRepository(session)
    payments = await repo.list_recent(tenant.id)
    return {"payments": payments}


@router.get("/rekey-report")
async def get_rekey_report(
    tenant: Tenant = Depends(get_portal_tenant),
    session: AsyncSession = Depends(get_db_session),
):
    repo = SqlAlchemyRekeyRepository(session)
    report = await repo.get_latest_report(tenant.id)
    if report is None:
        raise HTTPException(status_code=404, detail="No rekey report found for this tenant")

    resolutions = await repo.get_resolutions_for_report(report.id)
    created_at = ensure_aware_utc(report.created_at)
    return {
        "report_id": report.id,
        "created_at": created_at.isoformat() if created_at else None,
        "payload": report.payload,
        "resolutions": [
            {
                "variant_id": resolution.variant_id,
                "action": resolution.action,
                "status": resolution.status,
                "error": resolution.error,
                "note": resolution.note,
                "created_at": (
                    created_at_iso.isoformat()
                    if (created_at_iso := ensure_aware_utc(resolution.created_at))
                    else None
                ),
            }
            for resolution in resolutions
        ],
    }


async def _apply_resolution(
    shopify_client: ShopifyClient,
    *,
    action: str,
    variant_id: str,
    entry: dict[str, Any],
) -> tuple[str, str | None]:
    """Execute one resolution's side effect. Returns ``(status, error)``."""
    if action in ("keep", "ignore"):
        return "recorded", None

    if action == "delete":
        product_id = entry.get("product_id")
        if not product_id:
            return "failed", "missing product_id for this variant"
        try:
            await shopify_client.bulk_delete_variants(product_id, [variant_id])
        except _APPLY_ERRORS as exc:
            return "failed", f"{type(exc).__name__}: {exc}"
        return "applied", None

    if action == "approve_sku":
        product_id = entry.get("product_id")
        new_sku = entry.get("new_sku")
        if not product_id or not new_sku:
            return "failed", "no proposed SKU for this variant"
        try:
            await shopify_client.bulk_update_variants(
                product_id, [{"id": variant_id, "sku": new_sku}]
            )
        except _APPLY_ERRORS as exc:
            return "failed", f"{type(exc).__name__}: {exc}"
        return "applied", None

    raise ValueError(f"unknown action: {action}")  # pragma: no cover - guarded by pydantic


@router.post("/rekey-resolutions")
async def post_rekey_resolutions(
    body: ResolutionsIn,
    tenant: Tenant = Depends(get_portal_tenant),
    session: AsyncSession = Depends(get_db_session),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
):
    repo = SqlAlchemyRekeyRepository(session)
    report = await repo.get_latest_report(tenant.id)
    if report is None:
        raise HTTPException(status_code=404, detail="No rekey report found for this tenant")

    variant_index = _index_report_variants(report.payload)

    unknown = [item.variant_id for item in body.resolutions if item.variant_id not in variant_index]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"variant_id(s) not found in latest rekey report: {unknown}",
        )

    shopify_client = ShopifyClient(tenant)
    results: list[ResolutionResultOut] = []
    try:
        for item in body.resolutions:
            existing = await repo.get_resolution(report.id, item.variant_id)
            if existing is not None and existing.status != "failed":
                results.append(
                    ResolutionResultOut(
                        variant_id=item.variant_id,
                        action=existing.action,
                        status="already_resolved",
                    )
                )
                continue

            entry = variant_index[item.variant_id]
            status, error = await _apply_resolution(
                shopify_client, action=item.action, variant_id=item.variant_id, entry=entry
            )
            if error is not None:
                logger.warning(
                    "portal_rekey_resolution_failed",
                    tenant_slug=tenant.slug,
                    tenant_id=tenant.id,
                    variant_id=item.variant_id,
                    action=item.action,
                    error=error,
                )
            await repo.record_resolution(
                tenant_id=tenant.id,
                report_id=report.id,
                variant_id=item.variant_id,
                action=item.action,
                status=status,
                error=error,
                note=item.note,
            )
            results.append(
                ResolutionResultOut(
                    variant_id=item.variant_id, action=item.action, status=status, error=error
                )
            )
            await audit.log(
                actor="portal",
                action="rekey.resolution",
                entity="rekey_resolution",
                tenant_id=tenant.id,
                entity_id=item.variant_id,
                payload={
                    "report_id": report.id,
                    "action": item.action,
                    "status": status,
                },
            )
    finally:
        await shopify_client.aclose()

    return {"results": [result.model_dump() for result in results]}


@router.get("/sync-status")
async def get_portal_sync_status(
    tenant: Tenant = Depends(get_portal_tenant),
    session: AsyncSession = Depends(get_db_session),
):
    sync_state_repo = SqlAlchemySyncStateRepository(session)
    return await sync_state_repo.get_status(tenant.id)


@router.get("/audit")
async def get_portal_audit_log(
    tenant: Tenant = Depends(get_portal_tenant),
    session: AsyncSession = Depends(get_db_session),
    limit: int = DEFAULT_AUDIT_LIMIT,
):
    """Tenant-scoped activity feed for the merchant portal.

    Always scoped to the authenticated tenant (via `get_portal_tenant`) —
    there is no `tenant` query param, so a valid token can never be used to
    read another tenant's audit trail.
    """
    clamped_limit = max(1, min(limit, MAX_AUDIT_LIMIT))
    audit_repo = SqlAlchemyAuditLogger(session)
    entries = await audit_repo.list_entries(tenant_id=tenant.id, limit=clamped_limit)
    return {
        "entries": [
            {
                "id": entry.id,
                "actor": entry.actor,
                "action": entry.action,
                "entity": entry.entity,
                "entity_id": entry.entity_id,
                "payload": entry.payload,
                "created_at": ensure_aware_utc(entry.created_at).isoformat(),
            }
            for entry in entries
        ]
    }
