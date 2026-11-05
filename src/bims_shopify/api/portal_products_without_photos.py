"""Portal view of the live Shopify products that have no photos.

Source: a stored scan, not a live query. Walking ~11k Shopify products takes
minutes, so ``POST .../scan`` starts the read-only ``missing_photos`` ops job
(single-flight per tenant) and ``GET`` serves filtered, paginated slices of the
latest successful scan stored on the job row. Authentication reuses
``get_portal_tenant``; every read is keyed by the authenticated tenant's id.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.ops_job_repository import SqlAlchemyOpsJobReader
from bims_shopify.api.portal import get_portal_tenant
from bims_shopify.application.products_without_photos import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    build_view,
)
from bims_shopify.datetime_utils import ensure_aware_utc
from bims_shopify.domain.ops_job import OpsJobView
from bims_shopify.domain.products_without_photos import SCOPE_LIVE
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ops.job_runner import JobConflictError

from .deps import get_db_session

COMMAND = "missing_photos"
_RUNNING = ("queued", "running")
_MAX_QUERY_LENGTH = 100

router = APIRouter(prefix="/api/portal/{slug}/products-without-photos", tags=["portal"])


def _scan_state(latest: OpsJobView | None, last_ok: OpsJobView | None) -> dict[str, Any]:
    """The scan block. The raw job error is deliberately never exposed."""
    if latest is None:
        status = "never"
    elif latest.status in _RUNNING:
        status = "running"
    elif latest.status == "succeeded":
        status = "ready"
    else:
        status = "failed"
    scanned_at = last_ok.finished_at if last_ok is not None else None
    result = (last_ok.result or {}) if last_ok is not None else {}
    running = latest if status == "running" and latest is not None else None
    return {
        "status": status,
        "job_id": latest.id if latest is not None else None,
        "progress_done": running.progress_done if running else None,
        "progress_total": running.progress_total if running else None,
        "message": running.message if running else None,
        "scanned_at": ensure_aware_utc(scanned_at).isoformat() if scanned_at else None,
        "stock_as_of": result.get("stock_as_of"),
    }


@router.get("")
async def list_products_without_photos(
    scope: Literal["live", "all"] = SCOPE_LIVE,
    q: str = Query(default="", max_length=_MAX_QUERY_LENGTH),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    tenant: Tenant = Depends(get_portal_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    reader = SqlAlchemyOpsJobReader(session)
    latest = await reader.latest(tenant.id, COMMAND)
    last_ok = await reader.latest_succeeded(tenant.id, COMMAND)
    body: dict[str, Any] = {"scan": _scan_state(latest, last_ok)}
    if last_ok is None or last_ok.result is None:
        body.update(
            scope=scope, page=page, page_size=page_size, total=0, items=[], counts=None,
            truncated=False,
        )
        return body
    body.update(
        build_view(
            last_ok.result,
            scope=scope,
            query=q,
            page=page,
            page_size=page_size,
            shop_domain=tenant.shopify_shop_domain,
        )
    )
    return body


@router.post("/scan", status_code=202)
async def start_products_without_photos_scan(
    request: Request,
    tenant: Tenant = Depends(get_portal_tenant),
) -> dict[str, Any]:
    runner = getattr(request.app.state, "job_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Background jobs are not available")
    try:
        job = await runner.start_job(tenant.id, COMMAND, {})
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail="A scan is already running") from exc
    return {"job_id": job.id, "status": job.status}
