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
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.rekey_repository import SqlAlchemyRekeyRepository
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.adapters.shopify.client import ShopifyClient, ShopifyGraphQLError
from bims_shopify.datetime_utils import ensure_aware_utc
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger

from .deps import get_db_session, get_tenant_repository

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
) -> Tenant:
    """Resolve and authenticate the tenant addressed by the ``{slug}`` path segment.

    Looking the tenant up by slug (rather than by reversing the token) means
    a valid token for tenant A can never be accepted against tenant B's
    slug — there is no cross-tenant lookup path, only a same-tenant hash
    comparison.
    """
    tenant = await repo.get_by_slug(slug)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    presented = authorization.removeprefix("Bearer ").strip()
    if not presented:
        raise HTTPException(status_code=401, detail="Unauthorized")

    stored_hash = await repo.get_portal_token_hash(tenant.id)
    if not stored_hash:
        raise HTTPException(status_code=401, detail="Unauthorized")

    presented_hash = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(presented_hash, stored_hash):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return tenant


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
