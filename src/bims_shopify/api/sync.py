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
    ProgressFn,
    SyncInventoryToShopify,
)
from bims_shopify.application.sync_interval import PULL_MODE, PUSH_MODE, interval_floor_for
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
    force: bool = False,
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
        return await _do_run_sync(tenant, dry_run, session, full=full, force=force)


async def _do_run_sync(
    tenant,
    dry_run: bool,
    session: AsyncSession,
    full: bool = False,
    force: bool = False,
    progress: ProgressFn | None = None,
):
    """Core sync coroutine shared by the manual HTTP endpoint (kept for the
    scheduler / back-compat), the APScheduler tick (main.py), and the
    background ops job (ops/sync_job.py) -- so behavior never diverges
    between the three entry points. ``progress`` is optional and only used
    by the background job to report coarse phase progress."""
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
        # force=True is a stronger operator override: it implies full=True
        # (ignore the incremental watermark) AND also ignores the stored
        # product_hashes watermark below, so every BIMS product with
        # resolvable stock is treated as a delta and re-pushed to Shopify —
        # even if the hashes already match current BIMS stock (e.g. after a
        # store wipe+rebuild where Shopify's real inventory no longer
        # matches what BIMS last saw).
        since = None if (full or force) else ((last_run - INCREMENTAL_OVERLAP) if last_run else None)

        # What the run actually did, not what was requested: an incremental
        # tenant with no watermark yet still performs a full pull.
        mode = "full" if since is None else "incremental"

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
        # force=True ignores the stored watermark entirely: every product
        # with resolvable stock is diffed against an empty baseline, so it
        # is treated as changed and re-pushed. The real hashes are still
        # persisted below at the end of a successful (non-dry-run) run, so
        # subsequent incremental runs go back to normal behavior.
        previous_stocks = (
            {}
            if force
            else {sku: float(value) for sku, value in previous.items() if value}
        )

        run_started_at = datetime.now(UTC)
        try:
            result = await use_case.run(
                tenant, previous_stocks, since=since, dry_run=dry_run, force=force, progress=progress
            )
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
                payload={
                    "dry_run": dry_run,
                    "error": str(exc),
                    "report": use_case.last_report,
                },
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
                    "scanned": report.detail.get("scanned"),
                    "mode": mode,
                },
            )
            return {"dry_run": True, "mode": mode, "report": asdict(report)}

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
                    "report": use_case.last_report,
                },
            )
            # last_sync must NOT advance: nothing was pushed.
            return {
                "mode": mode,
                "scanned": use_case.last_report["scanned"],
                "report": use_case.last_report,
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
        await sync_state_repo.clear_last_error(tenant.id)
        # Real per-phase durations, persisted so a minimum safe scheduler
        # interval can be derived from measured runs instead of guessed.
        timings = {
            "catalog_pull_seconds": (
                erp.last_catalog_pull.duration_seconds if erp.last_catalog_pull else None
            ),
            "stock_fetch_seconds": erp.last_stock_fetch_seconds,
        }
        summary = {
            "status": "ok",
            "matched": len(deltas),
            "updated": len(deltas),
            "duration_seconds": duration,
            "ran_at": run_started_at.isoformat(),
            "mode": mode,
            "timings": timings,
            "catalog_pull": erp.last_catalog_pull.summary() if erp.last_catalog_pull else None,
            "scanned": use_case.last_report["scanned"],
            "report": use_case.last_report,
        }
        await sync_state_repo.set_last_run_summary(tenant.id, summary)
        await sync_state_repo.record_run_duration(tenant.id, duration, mode)
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
            payload={
                "matched": len(deltas),
                "updated": len(deltas),
                "scanned": use_case.last_report["scanned"],
                "mode": mode,
            },
        )
        return {
            "status": "ok",
            "mode": mode,
            "synced_deltas": len(deltas),
            "timings": timings,
            "scanned": use_case.last_report["scanned"],
            "report": use_case.last_report,
        }
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
    status = await sync_state_repo.get_status(tenant.id)
    floor = await interval_floor_for(sync_state_repo, tenant.id, PULL_MODE)
    status["sync_interval"] = {
        "configured_minutes": tenant.sync_interval_minutes,
        "min_safe_minutes": floor.minutes,
        "basis": floor.basis,
        "slowest_recent_seconds": floor.slowest_seconds,
        "runs_considered": floor.runs_considered,
        # Every pull is a full pull; the setting below no longer changes that.
        "sync_mode": floor.sync_mode,
        "sync_mode_setting": tenant.sync_mode.value,
    }
    push_floor = await interval_floor_for(sync_state_repo, tenant.id, PUSH_MODE)
    status["push_interval"] = {
        "configured_minutes": tenant.push_interval_minutes,
        "min_safe_minutes": push_floor.minutes,
        "basis": push_floor.basis,
        "slowest_recent_seconds": push_floor.slowest_seconds,
        "runs_considered": push_floor.runs_considered,
    }
    status["snapshot_max_age_minutes"] = tenant.snapshot_max_age_minutes
    return status


@router.get("/{tenant_slug}/reconciliation")
async def get_reconciliation(
    tenant_slug: str,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = _Depends(get_db_session),
):
    """Return the latest persisted BIMS-vs-Shopify reconciliation report.

    Serves the last ``catalog.reconciliation`` audit entry written by
    ``python -m bims_shopify.ops.catalog <slug> status``. Does not re-pull
    live data: a full BIMS pull takes minutes, too slow for a request.
    """
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    audit = SqlAlchemyAuditLogger(session)
    entry = await audit.get_latest(action="catalog.reconciliation", tenant_id=tenant.id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No reconciliation report yet")
    return {"created_at": entry.created_at.isoformat(), "payload": entry.payload}
