"""Manual sync trigger endpoint, sync status and the catalog snapshot metadata."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Depends as _Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.application.push_snapshot import snapshot_decline
from bims_shopify.application.sync_interval import (
    PULL_MODE,
    PUSH_MODE,
    RECONCILE_MODE,
    interval_floor_for,
)
from bims_shopify.application.sync_inventory import ProgressFn
from bims_shopify.logging import get_logger
from bims_shopify.sync_locks import CombinedLock

from .deps import get_db_session, get_tenant_repository, require_admin

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Depends(require_admin)])
logger = get_logger(__name__)

def _lock_for_tenant(request: Request, tenant_id: int) -> asyncio.Lock | CombinedLock:
    """Reuse the scheduler's per-tenant locks (pull and push together) so a
    manual run can never execute concurrently with a scheduled pull or push
    (or another manual run) for the same tenant."""
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
    """One-shot sync: pull now, then push now (``ops.sync_workers.do_run_sync``).

    Shared by the manual HTTP endpoint, the background ops job
    (ops/sync_job.py) and the post-activation dry run, so behavior never
    diverges between entry points. ``full`` is accepted for API compatibility
    and has no effect: every pull is already a full pull. ``progress`` is
    optional and only used by the background job.
    """
    from bims_shopify.ops.sync_workers import do_run_sync

    return await do_run_sync(tenant, dry_run, session, force=force, progress=progress)


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
    reconcile_floor = await interval_floor_for(sync_state_repo, tenant.id, RECONCILE_MODE)
    status["reconcile_interval"] = {
        "configured_minutes": tenant.reconcile_interval_minutes,
        "min_safe_minutes": reconcile_floor.minutes,
        "basis": reconcile_floor.basis,
        "slowest_recent_seconds": reconcile_floor.slowest_seconds,
        "runs_considered": reconcile_floor.runs_considered,
    }
    status["snapshot_max_age_minutes"] = tenant.snapshot_max_age_minutes
    return status


def _meta_view(meta, now: datetime) -> dict:
    """Snapshot metadata plus its age, for operators."""
    view = meta.as_dict()
    finished = meta.finished_at or meta.started_at
    view["age_minutes"] = round((now - finished).total_seconds() / 60, 1)
    return view


@router.get("/{tenant_slug}/snapshot")
async def get_snapshot(
    tenant_slug: str,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = _Depends(get_db_session),
):
    """The catalog snapshot the push worker reads, and what the workers last did.

    ``current`` is the snapshot a push would use (``null`` before the first
    pull lands); ``history`` lists the last pulls newest first, including
    rejected ones (``status: rejected`` with its ``rejection`` reason).
    ``usable_for_push`` / ``decline`` say whether the push worker would use the
    current snapshot right now and, if not, why. ``last_reconcile`` is the last
    reconciliation against Shopify's real quantities, with its ``drift`` count
    (a healthy system sits near zero). Cheap: metadata only.
    """
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    snapshots = SqlAlchemySnapshotRepository(session)
    now = datetime.now(UTC)
    current = await snapshots.get_current_meta(tenant.id)
    history = await snapshots.list_recent_meta(tenant.id, limit=10)
    status = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    verdict = snapshot_decline(current, tenant, now)
    return {
        "current": _meta_view(current, now) if current else None,
        "history": [_meta_view(meta, now) for meta in history],
        "last_pull": status["last_pull_outcome"],
        "last_push": status["last_push_outcome"],
        # Apart from the push on purpose: "the push had nothing to do" and
        # "the reconcile found N silently wrong SKUs" must never look alike.
        "last_reconcile": status["last_reconcile_outcome"],
        "max_age_minutes": tenant.snapshot_max_age_minutes,
        "usable_for_push": verdict is None,
        "decline": {"reason": verdict[0], **verdict[1]} if verdict else None,
        "settings": {
            "pull_interval_minutes": tenant.sync_interval_minutes,
            "push_interval_minutes": tenant.push_interval_minutes,
            "reconcile_interval_minutes": tenant.reconcile_interval_minutes,
            "snapshot_max_age_minutes": tenant.snapshot_max_age_minutes,
            "pull_max_drop_ratio": tenant.pull_max_drop_ratio,
        },
    }


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
