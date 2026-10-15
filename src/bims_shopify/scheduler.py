"""AsyncIOScheduler that periodically syncs every active tenant.

Each active tenant has its OWN interval job (``tenant-sync-{id}``) whose
period is the tenant's ``sync_interval_minutes``. A cheap reconcile job keeps
the set of jobs aligned with the stored tenants (new, deactivated, changed
interval), and the tenant API calls ``reschedule_tenant`` for an immediate
effect, so an interval change never needs a container restart.

A per-tenant asyncio.Lock prevents overlapping runs for the same tenant if a
previous run has not finished by the time the next tick fires.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger
from bims_shopify.ports.repositories import TenantRepository

logger = get_logger(__name__)

TenantSyncFn = Callable[[Tenant], Awaitable[None]]
#: Yields a tenant repository bound to a fresh, short-lived DB session.
RepositoryScope = Callable[[], AbstractAsyncContextManager[TenantRepository]]

RECONCILE_JOB_ID = "tenant-sync-reconcile"


def job_id_for(tenant_id: int) -> str:
    return f"tenant-sync-{tenant_id}"


class TenantSyncScheduler:
    """Runs each active tenant's sync on that tenant's own interval, skipping
    inactive tenants and tenants whose previous run is still in flight."""

    def __init__(
        self,
        tenant_repository: TenantRepository,
        sync_fn: TenantSyncFn,
        reconcile_interval_minutes: int = 1,
        repository_scope: RepositoryScope | None = None,
    ) -> None:
        self._tenant_repository = tenant_repository
        self._sync_fn = sync_fn
        self._reconcile_interval_minutes = reconcile_interval_minutes
        # Prefer a fresh session per lookup: a repository bound to one
        # long-lived AsyncSession serves stale identity-map rows, so a change
        # made through the API (another session) would never be seen. Without
        # a scope we fall back to the fixed repository, serialised by a lock
        # because one AsyncSession cannot be used by concurrent tasks.
        self._repository_scope = repository_scope or self._shared_repository_scope
        self._shared_repo_lock = asyncio.Lock()
        self._locks: dict[int, asyncio.Lock] = {}
        self._scheduler = AsyncIOScheduler()
        # asyncio only holds a weak reference to a task once it is created;
        # without keeping our own strong reference here it can be garbage
        # collected mid-run. Discard from the set once the task is done.
        self._background_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def _shared_repository_scope(self):
        async with self._shared_repo_lock:
            yield self._tenant_repository

    def _lock_for(self, tenant_id: int) -> asyncio.Lock:
        if tenant_id not in self._locks:
            self._locks[tenant_id] = asyncio.Lock()
        return self._locks[tenant_id]

    def lock_for_tenant(self, tenant_id: int) -> asyncio.Lock:
        """Public accessor so other entry points (e.g. the manual run
        endpoint) can share this scheduler's per-tenant lock and avoid
        running concurrently with a scheduled sync for the same tenant."""
        return self._lock_for(tenant_id)

    def _launch(self, tenant: Tenant) -> None:
        if not tenant.active or tenant.id is None:
            return
        lock = self._lock_for(tenant.id)
        if lock.locked():
            return
        task = asyncio.create_task(self._run_with_lock(lock, tenant))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def run_once(self) -> None:
        """Run every active tenant now, regardless of its interval."""
        async with self._repository_scope() as repo:
            tenants = await repo.list_active()
        for tenant in tenants:
            self._launch(tenant)

    async def run_tenant(self, tenant_id: int) -> None:
        """Interval tick for one tenant. Reads the tenant fresh from storage
        so settings changed since startup (sync mode, active flag) apply."""
        async with self._repository_scope() as repo:
            tenant = await repo.get_by_id(tenant_id)
        if tenant is None or not tenant.active:
            self.remove_tenant(tenant_id)
            return
        self._launch(tenant)

    async def _run_with_lock(self, lock: asyncio.Lock, tenant: Tenant) -> None:
        async with lock:
            await self._sync_fn(tenant)

    # -- job management -------------------------------------------------

    def reschedule_tenant(self, tenant: Tenant) -> None:
        """Create, re-time or drop this tenant's job to match ``tenant``.

        The job is only touched when its interval actually changed, so
        reconciling never resets a countdown that is already running."""
        if tenant.id is None:
            return
        if not tenant.active:
            self.remove_tenant(tenant.id)
            return
        interval = timedelta(minutes=tenant.sync_interval_minutes)
        job = self._scheduler.get_job(job_id_for(tenant.id))
        if job is None:
            self._scheduler.add_job(
                self.run_tenant,
                trigger=IntervalTrigger(minutes=tenant.sync_interval_minutes),
                args=[tenant.id],
                id=job_id_for(tenant.id),
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        elif job.trigger.interval != interval:
            self._scheduler.reschedule_job(
                job_id_for(tenant.id),
                trigger=IntervalTrigger(minutes=tenant.sync_interval_minutes),
            )
            logger.info(
                "tenant_sync_rescheduled",
                tenant_id=tenant.id,
                interval_minutes=tenant.sync_interval_minutes,
            )

    def remove_tenant(self, tenant_id: int) -> None:
        if self._scheduler.get_job(job_id_for(tenant_id)) is not None:
            self._scheduler.remove_job(job_id_for(tenant_id))

    async def reconcile(self) -> None:
        """Align the scheduled jobs with the active tenants in storage."""
        async with self._repository_scope() as repo:
            tenants = await repo.list_active()
        wanted: set[str] = set()
        for tenant in tenants:
            if tenant.id is None:
                continue
            wanted.add(job_id_for(tenant.id))
            self.reschedule_tenant(tenant)
        for job in self._scheduler.get_jobs():
            if job.id.startswith("tenant-sync-") and job.id not in wanted | {RECONCILE_JOB_ID}:
                self._scheduler.remove_job(job.id)

    async def _safe_reconcile(self) -> None:
        try:
            await self.reconcile()
        except Exception as exc:  # a DB blip must not kill the reconcile loop
            logger.error("tenant_sync_reconcile_failed", error=str(exc))

    def start(self) -> None:
        # next_run_time=now: build the per-tenant jobs immediately at boot
        # instead of waiting a full reconcile period.
        self._scheduler.add_job(
            self._safe_reconcile,
            trigger=IntervalTrigger(minutes=self._reconcile_interval_minutes),
            id=RECONCILE_JOB_ID,
            replace_existing=True,
            next_run_time=datetime.now(UTC),
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
