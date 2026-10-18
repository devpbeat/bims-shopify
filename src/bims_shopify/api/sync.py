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
from bims_shopify.application.run_history import CYCLE_MODE, PULL_MODE
from bims_shopify.application.snapshot_gate import snapshot_decline
from bims_shopify.application.sync_inventory import ProgressFn
from bims_shopify.logging import get_logger
from bims_shopify.sync_locks import CombinedLock

from .deps import get_db_session, get_tenant_repository, require_admin

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Depends(require_admin)])
logger = get_logger(__name__)

def _lock_for_tenant(request: Request, tenant_id: int) -> asyncio.Lock | CombinedLock:
    """Reuse the scheduler's per-tenant locks (pull and write together) so a
    manual run can never execute concurrently with a scheduled pull or write cycle
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
    """One-shot sync: pull now, then reconcile now (``ops.sync_workers.do_run_sync``).

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
    # How long the two workers really take: the evidence for whether one write cycle per
    # snapshot keeps up with the pull loop. A cycle slower than a pull means skipped snapshots.
    status["pull"] = {
        "min_gap_seconds": tenant.pull_min_gap_seconds,
        **_duration_view(await sync_state_repo.get_recent_durations(tenant.id, PULL_MODE)),
        # Every pull is a full pull; the setting below no longer changes that.
        "sync_mode_setting": tenant.sync_mode.value,
    }
    status["write_cycle"] = _duration_view(
        await sync_state_repo.get_recent_durations(tenant.id, CYCLE_MODE)
    )
    status["snapshot_max_age_minutes"] = tenant.snapshot_max_age_minutes
    return status


def _duration_view(durations: list[float]) -> dict:
    return {
        "runs_considered": len(durations),
        "slowest_recent_seconds": max(durations) if durations else None,
        "latest_seconds": durations[-1] if durations else None,
    }


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
    """The catalog snapshot the write cycle reads, and what the workers last did.

    ``current`` is the snapshot a write cycle would use (``null`` before the first
    pull lands); ``history`` lists the last pulls newest first, including
    rejected ones (``status: rejected`` with its ``rejection`` reason).
    ``usable_for_write`` / ``decline`` say whether a write cycle would use the
    current snapshot right now and, if not, why. ``last_reconcile`` is the last
    stock pass against Shopify's real quantities, with its ``drift`` count (the
    per-cycle health number: a healthy system sits near zero) and, under
    ``last_skip``, the last snapshot a still-running cycle had to skip. Cheap:
    metadata only.
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
        "last_reconcile": status["last_reconcile_outcome"],
        # What the scheduled cycle's stages last did, one entry per stage
        # (``create``: products created, variants added, conflicts, failures;
        # ``publish``: drafts published and the gates' skips), each with the
        # snapshot it judged and when.
        "last_heal": status["last_heal_outcome"],
        "max_age_minutes": tenant.snapshot_max_age_minutes,
        "usable_for_write": verdict is None,
        "decline": {"reason": verdict[0], **verdict[1]} if verdict else None,
        "settings": {
            "pull_min_gap_seconds": tenant.pull_min_gap_seconds,
            "snapshot_max_age_minutes": tenant.snapshot_max_age_minutes,
            "pull_max_drop_ratio": tenant.pull_max_drop_ratio,
            "auto_create_missing": tenant.auto_create_missing,
            "auto_publish": tenant.auto_publish,
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
