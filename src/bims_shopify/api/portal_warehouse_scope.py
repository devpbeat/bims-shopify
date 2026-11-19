"""Portal: which BIMS warehouses feed the storefront, and a sync button.

The merchant lists their BIMS warehouses (with ``synced`` / ``selected`` flags),
saves a selection, and starts a sync. The selection is stored in the tenant's
``field_mappings`` (``stock_figure_rule=warehouse_scope`` plus
``stock_scope_warehouse_ids``), so no schema change is involved.

* The BIMS call is the existing ``warehouses`` ops command (``run_warehouses``).
* Saving refuses an id BIMS does not list: under ``warehouse_scope`` BIMS is
  trusted literally, so a typo'd id would publish zero for the whole catalog.
* The sync is the existing ``sync`` ops job started through the same
  ``JobRunner`` (single-flight per tenant) -- the ops endpoint itself needs the
  platform admin token, which a merchant does not hold. Status reads the same
  ``ops_jobs`` row, like the products-without-photos scan.

Authentication reuses ``get_portal_tenant``; everything is keyed by the
authenticated tenant.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.bims.client import BIMSAPIError
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.ops_job_repository import SqlAlchemyOpsJobReader
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api.portal import get_portal_tenant
from bims_shopify.api.tenants import _reschedule_live, effective_stock_config
from bims_shopify.datetime_utils import ensure_aware_utc
from bims_shopify.domain.stock_consolidation import (
    STOCK_FIGURE_RULE_KEY,
    STOCK_SCOPE_WAREHOUSE_IDS_KEY,
    StockFigureRule,
    resolve_stock_scope_warehouse_ids,
    validate_stock_field_mappings,
)
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger
from bims_shopify.ops.catalog import run_warehouses
from bims_shopify.ops.job_runner import JobConflictError

from .deps import get_audit_logger, get_db_session, get_tenant_repository

SYNC_COMMAND = "sync"
_RUNNING = ("queued", "running")
#: A merchant-started sync is always a real, incremental-free run: no dry run, no
#: forced full pull, no lock override (those stay operator-only options).
_SYNC_OPTIONS = {"dry_run": False, "full": False, "force": False}

router = APIRouter(prefix="/api/portal/{slug}/warehouse-scope", tags=["portal"])
logger = get_logger(__name__)


class WarehouseScopeIn(BaseModel):
    #: Validated by the domain resolver (ints or int-like strings, positive).
    warehouse_ids: list[Any]


async def _bims_warehouses(tenant: Tenant) -> list[dict[str, Any]]:
    """The BIMS warehouses that have an id; 502 (no detail leaked) if BIMS fails."""
    try:
        listing = await run_warehouses(tenant, {})
    except (BIMSAPIError, httpx.HTTPError, TimeoutError):
        logger.warning("portal_warehouse_list_failed", tenant=tenant.slug)
        raise HTTPException(status_code=502, detail="Could not read the warehouses from BIMS") from None
    return [w for w in listing["warehouses"] if w.get("id") is not None]


def _view(tenant: Tenant, warehouses: list[dict[str, Any]]) -> dict[str, Any]:
    rule, selected = effective_stock_config(tenant.field_mappings or {})
    listed = {w["id"] for w in warehouses}
    return {
        "rule": rule,
        "active": rule == StockFigureRule.WAREHOUSE_SCOPE.value,
        "selected_ids": selected,
        "selected_ids_not_found": [wid for wid in selected if wid not in listed],
        "warehouses": [
            {
                "id": w["id"],
                "name": w.get("name"),
                "synced": bool(w.get("synced")),
                "selected": w["id"] in selected,
            }
            for w in warehouses
        ],
    }


@router.get("")
async def get_warehouse_scope(tenant: Tenant = Depends(get_portal_tenant)) -> dict[str, Any]:
    return _view(tenant, await _bims_warehouses(tenant))


@router.put("")
async def put_warehouse_scope(
    request: Request,
    body: WarehouseScopeIn,
    tenant: Tenant = Depends(get_portal_tenant),
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
) -> dict[str, Any]:
    try:
        selected = resolve_stock_scope_warehouse_ids({STOCK_SCOPE_WAREHOUSE_IDS_KEY: body.warehouse_ids})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "field": "warehouse_ids"}) from None
    if not selected:
        raise HTTPException(
            status_code=422,
            detail={"message": "Select at least one warehouse", "field": "warehouse_ids"},
        )

    warehouses = await _bims_warehouses(tenant)
    listed = {w["id"] for w in warehouses}
    unknown = [wid for wid in selected if wid not in listed]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "BIMS does not list these warehouses",
                "field": "warehouse_ids",
                "unknown_ids": unknown,
            },
        )

    previous_rule, previous_ids = effective_stock_config(tenant.field_mappings or {})
    mappings = {
        **(tenant.field_mappings or {}),
        STOCK_FIGURE_RULE_KEY: StockFigureRule.WAREHOUSE_SCOPE.value,
        STOCK_SCOPE_WAREHOUSE_IDS_KEY: selected,
    }
    validate_stock_field_mappings(mappings)
    updated = replace(tenant, field_mappings=mappings)
    if previous_rule != StockFigureRule.WAREHOUSE_SCOPE.value or previous_ids != selected:
        await repo.update(updated)
        await audit.log(
            actor="portal",
            action="portal.warehouse_scope_updated",
            entity="tenant",
            tenant_id=tenant.id,
            entity_id=tenant.slug,
            # Rule and warehouse ids only: no credentials, no other mappings.
            payload={
                "previous_rule": previous_rule,
                "previous_warehouse_ids": previous_ids,
                "warehouse_ids": selected,
            },
        )
        _reschedule_live(request, updated)
    return _view(updated, warehouses)


def _sync_state(latest: Any) -> dict[str, Any]:
    """The sync block. The raw job error is deliberately never exposed."""
    if latest is None:
        status = "never"
    elif latest.status in _RUNNING:
        status = "running"
    elif latest.status == "succeeded":
        status = "succeeded"
    else:
        status = "failed"
    running = status == "running"
    finished = latest.finished_at if latest is not None else None
    return {
        "status": status,
        "job_id": latest.id if latest is not None else None,
        "progress_done": latest.progress_done if running else None,
        "progress_total": latest.progress_total if running else None,
        "message": latest.message if running else None,
        "finished_at": ensure_aware_utc(finished).isoformat() if finished else None,
    }


@router.get("/sync")
async def get_warehouse_scope_sync(
    tenant: Tenant = Depends(get_portal_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    return _sync_state(await SqlAlchemyOpsJobReader(session).latest(tenant.id, SYNC_COMMAND))


@router.post("/sync", status_code=202)
async def start_warehouse_scope_sync(
    request: Request,
    tenant: Tenant = Depends(get_portal_tenant),
) -> dict[str, Any]:
    runner = getattr(request.app.state, "job_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Background jobs are not available")
    try:
        job = await runner.start_job(tenant.id, SYNC_COMMAND, dict(_SYNC_OPTIONS))
    except JobConflictError:
        raise HTTPException(status_code=409, detail="A sync is already running") from None
    return {"job_id": job.id, "status": job.status}
