"""AsyncIOScheduler running each active tenant's inventory-sync workers.

Every active tenant has THREE interval jobs, independent of each other:

* ``tenant-pull-{id}``: refresh the BIMS catalog snapshot, every
  ``sync_interval_minutes``. It takes as long as BIMS takes.
* ``tenant-push-{id}``: push snapshot differences to Shopify, every
  ``push_interval_minutes``. It never calls BIMS and takes seconds.
* ``tenant-reconcile-{id}``: compare Shopify's REAL quantities with the
  snapshot and correct what differs, every ``reconcile_interval_minutes``
  (a few times a day: it pages the whole Shopify catalog). It holds the push
  lock while it runs, so it never overlaps the push: a push tick stands aside
  (recorded), and a reconcile tick that finds a push running waits for it.

A cheap reconcile job keeps the set of jobs aligned with the stored tenants
(new, deactivated, changed interval), and the tenant API calls
``reschedule_tenant`` for an immediate effect, so an interval change never
needs a container restart.

Each worker has its own per-tenant lock, so a worker never overlaps itself: a
tick that finds the previous run of the same worker still going is SKIPPED
(not queued) and the skip is recorded. A slow pull never blocks the push.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger
from bims_shopify.ports.repositories import TenantRepository
from bims_shopify.sync_locks import CombinedLock, SyncLocks

logger = get_logger(__name__)

TenantSyncFn = Callable[[Tenant], Awaitable[None]]
#: Called when a tick finds the previous run of the same worker still going.
SkipFn = Callable[[str, Tenant], Awaitable[None]]
#: When did this tenant's catalog pull last run (None: never)?
LastPullFn = Callable[[Tenant], Awaitable[datetime | None]]
#: When did this tenant's reconcile last run (None: never)?
LastReconcileFn = Callable[[Tenant], Awaitable[datetime | None]]
#: Yields a tenant repository bound to a fresh, short-lived DB session.
RepositoryScope = Callable[[], AbstractAsyncContextManager[TenantRepository]]

RECONCILE_JOB_ID = "tenant-sync-reconcile"

PULL = "pull"
PUSH = "push"
RECONCILE = "reconcile"

#: A newly created reconcile job first fires no sooner than this after boot, so
#: a deploy does not start a catalog-wide scan while the app is still settling
#: (and the pull is typically running).
RECONCILE_STARTUP_GRACE = timedelta(minutes=5)

_JOB_PREFIXES = ("tenant-pull-", "tenant-push-", "tenant-reconcile-")


@dataclass(frozen=True)
class _Worker:
    kind: str
    interval_minutes: Callable[[Tenant], int]


_WORKERS = {
    PULL: _Worker(PULL, lambda tenant: tenant.sync_interval_minutes),
    PUSH: _Worker(PUSH, lambda tenant: tenant.push_interval_minutes),
    RECONCILE: _Worker(RECONCILE, lambda tenant: tenant.reconcile_interval_minutes),
}


def job_id_for(tenant_id: int, kind: str = PULL) -> str:
    return f"tenant-{kind}-{tenant_id}"


class TenantSyncScheduler:
    """Runs each active tenant's pull and push workers on that tenant's own
    intervals, skipping inactive tenants and ticks whose previous run of the
    same worker is still in flight."""

    def __init__(
        self,
        tenant_repository: TenantRepository,
        sync_fn: TenantSyncFn,
        reconcile_interval_minutes: int = 1,
        repository_scope: RepositoryScope | None = None,
        *,
        push_fn: TenantSyncFn | None = None,
        skip_fn: SkipFn | None = None,
        last_pull_fn: LastPullFn | None = None,
        reconcile_fn: TenantSyncFn | None = None,
        last_reconcile_fn: LastReconcileFn | None = None,
    ) -> None:
        self._tenant_repository = tenant_repository
        #: ``sync_fn`` is the PULL worker (the name predates the split).
        self._fns: dict[str, TenantSyncFn | None] = {
            PULL: sync_fn,
            PUSH: push_fn,
            RECONCILE: reconcile_fn,
        }
        self._skip_fn = skip_fn
        self._last_pull_fn = last_pull_fn
        self._last_reconcile_fn = last_reconcile_fn
        self._reconcile_interval_minutes = reconcile_interval_minutes
        # Prefer a fresh session per lookup: a repository bound to one
        # long-lived AsyncSession serves stale identity-map rows, so a change
        # made through the API (another session) would never be seen. Without
        # a scope we fall back to the fixed repository, serialised by a lock
        # because one AsyncSession cannot be used by concurrent tasks.
        self._repository_scope = repository_scope or self._shared_repository_scope
        self._shared_repo_lock = asyncio.Lock()
        self.locks = SyncLocks()
        self._scheduler = AsyncIOScheduler()
        # asyncio only holds a weak reference to a task once it is created;
        # without keeping our own strong reference here it can be garbage
        # collected mid-run. Discard from the set once the task is done.
        self._background_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def _shared_repository_scope(self):
        async with self._shared_repo_lock:
            yield self._tenant_repository

    def lock_for_tenant(self, tenant_id: int) -> CombinedLock:
        """Both workers' locks, for entry points that run the pull AND the push
        (the manual one-shot, the ``sync`` ops job): while held, scheduled ticks
        of either worker stand aside, and it waits for a running tick first."""
        return self.locks.combined(tenant_id)

    def reconcile_lock_for_tenant(self, tenant_id: int) -> CombinedLock:
        """The reconcile lock and the push lock, for an on-demand reconcile (the
        ``reconcile`` ops job): while held, scheduled pushes and reconciles
        stand aside, and it waits for a running push first."""
        return self.locks.reconcile_worker(tenant_id)

    def _lock_for(self, kind: str, tenant_id: int) -> asyncio.Lock:
        if kind == PULL:
            return self.locks.pull(tenant_id)
        if kind == RECONCILE:
            return self.locks.reconcile(tenant_id)
        return self.locks.push(tenant_id)

    async def _launch(self, kind: str, tenant: Tenant) -> None:
        fn = self._fns[kind]
        if fn is None or not tenant.active or tenant.id is None:
            return
        lock = self._lock_for(kind, tenant.id)
        if lock.locked():
            await self._record_skip(kind, tenant)
            return
        # Take the lock HERE, before yielding to the loop: a free asyncio.Lock
        # is acquired without suspending, so two simultaneous ticks cannot both
        # pass the check above and start overlapping runs.
        await lock.acquire()
        run = self._run_reconcile if kind == RECONCILE else self._run_holding
        task = asyncio.create_task(run(lock, fn, tenant))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _record_skip(self, kind: str, tenant: Tenant) -> None:
        logger.warning(
            "worker_tick_skipped", worker=kind, tenant_id=tenant.id, tenant_slug=tenant.slug
        )
        if self._skip_fn is None:
            return
        try:
            await self._skip_fn(kind, tenant)
        except Exception as exc:  # recording a skip must never break the tick loop
            logger.error("worker_skip_record_failed", worker=kind, tenant_id=tenant.id, error=str(exc))

    @staticmethod
    async def _run_holding(lock: asyncio.Lock, fn: TenantSyncFn, tenant: Tenant) -> None:
        try:
            await fn(tenant)
        finally:
            lock.release()

    async def _run_reconcile(
        self, reconcile_lock: asyncio.Lock, fn: TenantSyncFn, tenant: Tenant
    ) -> None:
        """Run a reconcile once the push lock is ours too (waiting for a push
        in flight rather than skipping: it only runs a few times a day, and a
        push is over in seconds). ``reconcile_lock`` was taken at launch."""
        try:
            assert tenant.id is not None
            async with self.locks.push(tenant.id):
                await fn(tenant)
        finally:
            reconcile_lock.release()

    async def run_once(self) -> None:
        """Run every active tenant's workers now, regardless of their intervals."""
        async with self._repository_scope() as repo:
            tenants = await repo.list_active()
        for tenant in tenants:
            await self._launch(PULL, tenant)
            await self._launch(PUSH, tenant)

    async def _tick(self, kind: str, tenant_id: int) -> None:
        """Interval tick for one worker. Reads the tenant fresh from storage
        so settings changed since startup (active flag, thresholds) apply."""
        async with self._repository_scope() as repo:
            tenant = await repo.get_by_id(tenant_id)
        if tenant is None or not tenant.active:
            self.remove_tenant(tenant_id)
            return
        await self._launch(kind, tenant)

    async def run_tenant(self, tenant_id: int) -> None:
        """Pull tick for one tenant."""
        await self._tick(PULL, tenant_id)

    async def run_push_tenant(self, tenant_id: int) -> None:
        """Push tick for one tenant."""
        await self._tick(PUSH, tenant_id)

    async def run_reconcile_tenant(self, tenant_id: int) -> None:
        """Reconcile tick for one tenant."""
        await self._tick(RECONCILE, tenant_id)

    # -- job management -------------------------------------------------

    def reschedule_tenant(
        self,
        tenant: Tenant,
        *,
        next_pull_at: datetime | None = None,
        next_reconcile_at: datetime | None = None,
    ) -> None:
        """Create, re-time or drop this tenant's jobs to match ``tenant``.

        A job is only touched when its interval actually changed, so
        reconciling never resets a countdown that is already running.
        ``next_pull_at`` / ``next_reconcile_at`` set when a NEWLY created pull /
        reconcile job first fires."""
        if tenant.id is None:
            return
        if not tenant.active:
            self.remove_tenant(tenant.id)
            return
        for kind, worker in _WORKERS.items():
            if self._fns[kind] is None:
                continue
            minutes = worker.interval_minutes(tenant)
            job_id = job_id_for(tenant.id, kind)
            job = self._scheduler.get_job(job_id)
            target = {
                PULL: self.run_tenant,
                PUSH: self.run_push_tenant,
                RECONCILE: self.run_reconcile_tenant,
            }[kind]
            if job is None:
                kwargs = {}
                if kind == PULL and next_pull_at is not None:
                    kwargs["next_run_time"] = next_pull_at
                if kind == RECONCILE and next_reconcile_at is not None:
                    kwargs["next_run_time"] = next_reconcile_at
                self._scheduler.add_job(
                    target,
                    trigger=IntervalTrigger(minutes=minutes),
                    args=[tenant.id],
                    id=job_id,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    **kwargs,
                )
            elif job.trigger.interval != timedelta(minutes=minutes):
                self._scheduler.reschedule_job(job_id, trigger=IntervalTrigger(minutes=minutes))
                logger.info(
                    "tenant_worker_rescheduled",
                    worker=kind,
                    tenant_id=tenant.id,
                    interval_minutes=minutes,
                )

    def remove_tenant(self, tenant_id: int) -> None:
        for kind in _WORKERS:
            job_id = job_id_for(tenant_id, kind)
            if self._scheduler.get_job(job_id) is not None:
                self._scheduler.remove_job(job_id)

    async def _first_pull_time(self, tenant: Tenant) -> datetime | None:
        """When a newly created pull job should first fire.

        The APScheduler default is one full interval from creation, which on
        every restart would delay (or, with frequent deploys, starve) the pull.
        With the last pull time known: ``last + interval``, or now if overdue
        or never pulled. ``None`` keeps the default."""
        if self._last_pull_fn is None:
            return None
        now = datetime.now(UTC)
        try:
            last = await self._last_pull_fn(tenant)
        except Exception as exc:  # fall back to the default rather than fail reconcile
            logger.warning("last_pull_lookup_failed", tenant_id=tenant.id, error=str(exc))
            return None
        if last is None:
            return now
        return max(now, last + timedelta(minutes=tenant.sync_interval_minutes))

    async def _first_reconcile_time(self, tenant: Tenant) -> datetime | None:
        """When a newly created reconcile job should first fire.

        Like the pull, the APScheduler default (one full interval from
        creation) would push a reconcile back by hours on every restart, and
        starve it with frequent deploys. With the last run known:
        ``last + interval``; never run, or overdue: after the startup grace.
        ``None`` keeps the default."""
        if self._last_reconcile_fn is None:
            return None
        earliest = datetime.now(UTC) + RECONCILE_STARTUP_GRACE
        try:
            last = await self._last_reconcile_fn(tenant)
        except Exception as exc:  # fall back to the default rather than fail reconcile
            logger.warning("last_reconcile_lookup_failed", tenant_id=tenant.id, error=str(exc))
            return None
        if last is None:
            return earliest
        return max(earliest, last + timedelta(minutes=tenant.reconcile_interval_minutes))

    async def reconcile(self) -> None:
        """Align the scheduled jobs with the active tenants in storage."""
        async with self._repository_scope() as repo:
            tenants = await repo.list_active()
        wanted: set[str] = set()
        for tenant in tenants:
            if tenant.id is None:
                continue
            for kind in _WORKERS:
                wanted.add(job_id_for(tenant.id, kind))
            next_pull_at = None
            if self._scheduler.get_job(job_id_for(tenant.id, PULL)) is None:
                next_pull_at = await self._first_pull_time(tenant)
            next_reconcile_at = None
            if self._scheduler.get_job(job_id_for(tenant.id, RECONCILE)) is None:
                next_reconcile_at = await self._first_reconcile_time(tenant)
            self.reschedule_tenant(
                tenant, next_pull_at=next_pull_at, next_reconcile_at=next_reconcile_at
            )
        for job in self._scheduler.get_jobs():
            if job.id.startswith(_JOB_PREFIXES) and job.id not in wanted:
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
