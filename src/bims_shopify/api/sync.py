"""Manual sync trigger endpoint."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Depends as _Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.application.sync_inventory import (
    DryRunReport,
    NeedsConfirmation,
    SyncInventoryToShopify,
)
from bims_shopify.logging import get_logger

from .deps import get_db_session, get_tenant_repository, require_admin

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Depends(require_admin)])
logger = get_logger(__name__)

# Re-pull products modified within this window before the last successful
# sync, to tolerate clock skew / near-miss writes on the BIMS side.
INCREMENTAL_OVERLAP = timedelta(minutes=5)


def _lock_for_tenant(request: Request, tenant_id: int) -> asyncio.Lock:
    """Reuse the scheduler's per-tenant lock so a manual run can never
    execute concurrently with the scheduled job (or another manual run)
    for the same tenant."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        return scheduler.lock_for_tenant(tenant_id)
    locks: dict[int, asyncio.Lock] = request.app.state.__dict__.setdefault(
        "_manual_sync_locks", {}
    )
    if tenant_id not in locks:
        locks[tenant_id] = asyncio.Lock()
    return locks[tenant_id]


@router.post("/{tenant_slug}/run")
async def run_sync(
    tenant_slug: str,
    request: Request,
    dry_run: bool = True,
    full: bool = False,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = _Depends(get_db_session),
):
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    lock = _lock_for_tenant(request, tenant.id)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A sync run is already in progress for this tenant",
        )

    async with lock:
        return await _do_run_sync(tenant, dry_run, session, full=full)


async def _do_run_sync(tenant, dry_run: bool, session: AsyncSession, full: bool = False):
    sync_state_repo = SqlAlchemySyncStateRepository(session)
    audit = SqlAlchemyAuditLogger(session)
    client = BIMSClient(tenant)
    started_at = datetime.now(UTC)
    try:
        erp = BIMSERPAdapter(client)
        storefront = ShopifyClient(tenant)
        use_case = SyncInventoryToShopify(erp, storefront)

        last_run = await sync_state_repo.get_last_run(tenant.id)
        # full=True forces a complete catalog pull (e.g. initial stock push
        # after a catalog import), ignoring the incremental watermark.
        since = None if full else ((last_run - INCREMENTAL_OVERLAP) if last_run else None)

        if since is not None:
            try:
                deleted_ids = await erp.list_deleted_product_ids(tenant, since)
            except Exception as exc:  # best-effort, must not abort the sync
                logger.warning(
                    "sync_deleted_products_check_failed",
                    tenant_slug=tenant.slug,
                    tenant_id=tenant.id,
                    error=str(exc),
                )
                deleted_ids = []
            if deleted_ids:
                # Not yet wired to unpublish/zero-out the matching Shopify
                # variants automatically; surfaced so a human can reconcile
                # until that flow is implemented.
                logger.warning(
                    "sync_deleted_products_detected_unhandled",
                    tenant_slug=tenant.slug,
                    tenant_id=tenant.id,
                    deleted_count=len(deleted_ids),
                    deleted_ids=deleted_ids[:20],
                )

        previous = await sync_state_repo.get_product_hashes(tenant.id)
        previous_stocks = {sku: float(value) for sku, value in previous.items() if value}

        run_started_at = datetime.now(UTC)
        try:
            result = await use_case.run(tenant, previous_stocks, since=since, dry_run=dry_run)
        except Exception as exc:
            duration = (datetime.now(UTC) - started_at).total_seconds()
            logger.error(
                "sync_run_failed",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                dry_run=dry_run,
                duration_seconds=duration,
                error=str(exc),
            )
            await sync_state_repo.set_last_error(tenant.id, str(exc))
            await audit.log(
                actor="system",
                action="sync.error",
                entity="sync_run",
                tenant_id=tenant.id,
                payload={"dry_run": dry_run, "error": str(exc)},
            )
            raise

        duration = (datetime.now(UTC) - started_at).total_seconds()

        if dry_run:
            report: DryRunReport = result  # type: ignore[assignment]
            logger.info(
                "sync_run_dry_run",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                total_products=report.total_products,
                would_update=report.would_update,
                skipped_unresolved_stock=report.skipped_unresolved_stock,
                duration_seconds=duration,
            )
            await audit.log(
                actor="system",
                action="sync.dry_run",
                entity="sync_run",
                tenant_id=tenant.id,
                payload={
                    "total_products": report.total_products,
                    "would_update": report.would_update,
                    "skipped_unresolved_stock": report.skipped_unresolved_stock,
                },
            )
            return {"dry_run": True, "report": asdict(report)}

        if isinstance(result, NeedsConfirmation):
            logger.warning(
                "sync_run_needs_confirmation",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                reason=result.reason,
                zero_count=result.zero_count,
                matched_count=result.matched_count,
                threshold=result.threshold,
                duration_seconds=duration,
            )
            await audit.log(
                actor="system",
                action="sync.needs_confirmation",
                entity="sync_run",
                tenant_id=tenant.id,
                payload={
                    "reason": result.reason,
                    "zero_count": result.zero_count,
                    "matched_count": result.matched_count,
                    "threshold": result.threshold,
                },
            )
            # last_sync must NOT advance: nothing was pushed.
            return {
                "status": "needs_confirmation",
                "reason": result.reason,
                "zero_count": result.zero_count,
                "matched_count": result.matched_count,
                "threshold": result.threshold,
            }

        deltas = result
        await sync_state_repo.set_product_hashes(
            tenant.id, {delta.sku: str(delta.new_stock) for delta in deltas} | previous
        )
        # last_sync only advances after the full run (including the push)
        # succeeded end-to-end.
        await sync_state_repo.set_last_run(tenant.id, run_started_at)
        summary = {
            "status": "ok",
            "matched": len(deltas),
            "updated": len(deltas),
            "duration_seconds": duration,
            "ran_at": run_started_at.isoformat(),
        }
        await sync_state_repo.set_last_run_summary(tenant.id, summary)
        logger.info(
            "sync_run_completed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            matched=len(deltas),
            updated=len(deltas),
            duration_seconds=duration,
        )
        await audit.log(
            actor="system",
            action="sync.completed",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={"matched": len(deltas), "updated": len(deltas)},
        )
        return {"status": "ok", "synced_deltas": len(deltas)}
    finally:
        await client.aclose()


@router.get("/{tenant_slug}/status")
async def get_sync_status(
    tenant_slug: str,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = _Depends(get_db_session),
):
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    sync_state_repo = SqlAlchemySyncStateRepository(session)
    return await sync_state_repo.get_status(tenant.id)
