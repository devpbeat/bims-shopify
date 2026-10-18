"""Runs each active tenant's inventory-sync workers: a continuous pull loop, and one
write cycle per new snapshot.

Every active tenant has ONE long-lived task, its pull loop:

    pull -> (a new snapshot landed) -> hand it to a write cycle -> pull again

* The pull is paced by its own duration: as soon as one finishes the next starts. There is
  no fixed cadence. A per-tenant MINIMUM gap (``pull_min_gap_seconds``, live-editable)
  between the end of a pull and the start of the next keeps a pull that answers instantly,
  with garbage or an error, from becoming a hot loop against BIMS.
* A pull never overlaps itself: the loop is sequential, and the per-tenant pull lock also
  keeps it from running beside a manual sync (a pull that finds the lock taken is
  recorded as skipped and tried again after the gap).
* The write cycle (stock + self-healing stages) is not on a timer. It is launched, as its
  own task, when and only when a pull lands a new snapshot. If the previous cycle is still
  running (reading ~28k Shopify variants can outlast a pull) the snapshot is SKIPPED, not
  queued: the skip is recorded and the next snapshot gets its own cycle. Work never stacks.

A cheap alignment job keeps the set of loops aligned with the stored tenants (new,
deactivated), and the tenant API calls ``reschedule_tenant`` so a changed setting applies
at once, without a container restart.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger
from bims_shopify.ports.repositories import TenantRepository
from bims_shopify.sync_locks import CombinedLock, SyncLocks

logger = get_logger(__name__)

#: The pull worker. Returns True when a NEW snapshot landed (so a write cycle is due);
#: False for a rejected or failed pull, which lands nothing.
PullFn = Callable[[Tenant], Awaitable[bool]]
#: The write cycle worker.
CycleFn = Callable[[Tenant], Awaitable[None]]
#: Called when a pull or a cycle finds the previous run of the same worker still going.
SkipFn = Callable[[str, Tenant], Awaitable[None]]
#: When did this tenant's catalog pull last finish (None: never)?
LastPullFn = Callable[[Tenant], Awaitable[datetime | None]]
#: Yields a tenant repository bound to a fresh, short-lived DB session.
RepositoryScope = Callable[[], AbstractAsyncContextManager[TenantRepository]]

ALIGN_JOB_ID = "tenant-sync-align"

PULL = "pull"
CYCLE = "cycle"

#: A loop waiting out its gap wakes at least this often to re-read its tenant, so a
#: deactivated tenant, or a gap edited without ``reschedule_tenant``, is noticed.
MAX_WAIT_SECONDS = 30.0


class TenantSyncScheduler:
    """Runs each active tenant's pull loop and the write cycles it triggers, skipping
    inactive tenants and never overlapping a worker with itself."""

    def __init__(
        self,
        tenant_repository: TenantRepository,
        pull_fn: PullFn,
        repository_scope: RepositoryScope | None = None,
        *,
        cycle_fn: CycleFn | None = None,
        skip_fn: SkipFn | None = None,
        last_pull_fn: LastPullFn | None = None,
        align_interval_minutes: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._tenant_repository = tenant_repository
        self._pull_fn = pull_fn
        self._cycle_fn = cycle_fn
        self._skip_fn = skip_fn
        self._last_pull_fn = last_pull_fn
        self._align_interval_minutes = align_interval_minutes
        self._clock = clock
        # Prefer a fresh session per lookup: a repository bound to one
        # long-lived AsyncSession serves stale identity-map rows, so a change
        # made through the API (another session) would never be seen. Without
        # a scope we fall back to the fixed repository, serialised by a lock
        # because one AsyncSession cannot be used by concurrent tasks.
        self._repository_scope = repository_scope or self._shared_repository_scope
        self._shared_repo_lock = asyncio.Lock()
        self.locks = SyncLocks()
        self._scheduler = AsyncIOScheduler()
        self._loops: dict[int, asyncio.Task[None]] = {}
        self._wake: dict[int, asyncio.Event] = {}
        # asyncio only holds a weak reference to a task once it is created;
        # without keeping our own strong reference here it can be garbage
        # collected mid-run. Discard from the set once the task is done.
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._sleep_override = sleep

    @asynccontextmanager
    async def _shared_repository_scope(self):
        async with self._shared_repo_lock:
            yield self._tenant_repository

    def lock_for_tenant(self, tenant_id: int) -> CombinedLock:
        """Both workers' locks, for entry points that run the pull AND the write
        (the manual one-shot, the ``sync`` ops job): while held, the pull loop and
        the write cycles stand aside, and it waits for a running one first."""
        return self.locks.combined(tenant_id)

    def write_lock_for_tenant(self, tenant_id: int) -> asyncio.Lock:
        """The write lock, for an on-demand write (the ``reconcile`` ops job): while
        held, scheduled write cycles stand aside (recorded as skipped)."""
        return self.locks.write(tenant_id)

    # -- the pull loop ------------------------------------------------------

    async def _wait(self, tenant_id: int, seconds: float) -> None:
        """Sleep up to ``seconds`` (at most ``MAX_WAIT_SECONDS`` per call), waking early
        when ``reschedule_tenant`` pokes the loop."""
        seconds = min(seconds, MAX_WAIT_SECONDS)
        if self._sleep_override is not None:
            await self._sleep_override(seconds)
            return
        event = self._wake.setdefault(tenant_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=seconds)
        event.clear()

    async def _get_tenant(self, tenant_id: int) -> Tenant | None:
        async with self._repository_scope() as repo:
            return await repo.get_by_id(tenant_id)

    async def _initial_delay(self, tenant: Tenant) -> float:
        """Seconds until the first pull after (re)start: the minimum gap since the last
        pull finished, so frequent deploys cannot hammer BIMS; none when overdue or
        never pulled."""
        if self._last_pull_fn is None:
            return 0.0
        try:
            last = await self._last_pull_fn(tenant)
        except Exception as exc:  # fall back to pulling at once rather than fail the loop
            logger.warning("last_pull_lookup_failed", tenant_id=tenant.id, error=str(exc))
            return 0.0
        if last is None:
            return 0.0
        elapsed = (datetime.now(UTC) - last).total_seconds()
        return max(0.0, tenant.pull_min_gap_seconds - elapsed)

    async def _pull_loop(self, tenant_id: int) -> None:
        """Pull, hand the new snapshot to a write cycle, pull again; never overlaps itself."""
        not_before: float | None = None  # first pull of this loop may not start earlier
        last_end: float | None = None  # monotonic time the previous pull (attempt) ended
        skipping = False
        while True:
            try:
                tenant = await self._get_tenant(tenant_id)
            except Exception as exc:  # a DB blip must not kill the loop
                logger.error("pull_loop_tenant_lookup_failed", tenant_id=tenant_id, error=str(exc))
                await self._wait(tenant_id, MAX_WAIT_SECONDS)
                continue
            if tenant is None or not tenant.active:
                self._loops.pop(tenant_id, None)
                return
            if not_before is None:
                not_before = self._clock() + await self._initial_delay(tenant)
            # The gap is read from the tenant on every pass, so lowering or raising it
            # applies to the wait already in progress.
            ready_at = not_before if last_end is None else last_end + tenant.pull_min_gap_seconds
            remaining = ready_at - self._clock()
            if remaining > 0:
                await self._wait(tenant_id, remaining)
                continue

            lock = self.locks.pull(tenant_id)
            if lock.locked():
                # A manual sync owns the pull lock. Not queued: note it once per streak and
                # look again after the gap.
                if not skipping:
                    skipping = True
                    await self._record_skip(PULL, tenant)
                last_end = self._clock()
                continue
            skipping = False
            await lock.acquire()
            landed = False
            try:
                landed = bool(await self._pull_fn(tenant))
            except Exception as exc:  # one failing pull must not stop the loop
                logger.error("pull_loop_pull_failed", tenant_id=tenant_id, error=str(exc))
            finally:
                lock.release()
                # The gap runs from the END of the pull: a pull that took a second is
                # followed by a pause, one that took ten minutes by (at least) the gap.
                last_end = self._clock()
            if landed:
                await self._launch_cycle(tenant)

    # -- the write cycle ----------------------------------------------------

    async def _launch_cycle(self, tenant: Tenant) -> None:
        """Start a write cycle for the snapshot that just landed, as its own task so the
        pull loop is never held up by it. If the previous cycle (or a manual run) still
        holds the write lock, this snapshot is skipped and the skip recorded."""
        if self._cycle_fn is None or tenant.id is None:
            return
        lock = self.locks.write(tenant.id)
        if lock.locked():
            await self._record_skip(CYCLE, tenant)
            return
        # Take the lock HERE, before yielding to the loop: a free asyncio.Lock is acquired
        # without suspending, so the check above and the acquire cannot interleave.
        await lock.acquire()
        task = asyncio.create_task(self._run_cycle(lock, tenant))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_cycle(self, lock: asyncio.Lock, tenant: Tenant) -> None:
        assert self._cycle_fn is not None
        try:
            await self._cycle_fn(tenant)
        except Exception as exc:  # the cycle function reports its own failures; belt and braces
            logger.error("write_cycle_failed", tenant_id=tenant.id, error=str(exc))
        finally:
            lock.release()

    async def _record_skip(self, kind: str, tenant: Tenant) -> None:
        logger.warning(
            "worker_run_skipped", worker=kind, tenant_id=tenant.id, tenant_slug=tenant.slug
        )
        if self._skip_fn is None:
            return
        try:
            await self._skip_fn(kind, tenant)
        except Exception as exc:  # recording a skip must never break the loop
            logger.error("worker_skip_record_failed", worker=kind, tenant_id=tenant.id, error=str(exc))

    # -- loop management ----------------------------------------------------

    def reschedule_tenant(self, tenant: Tenant) -> None:
        """Start, wake or stop this tenant's pull loop to match ``tenant``.

        A running loop re-reads the tenant (gap, active flag) every iteration, so waking
        it is all a settings change needs: it never resets a pull in progress."""
        if tenant.id is None:
            return
        if not tenant.active:
            self.remove_tenant(tenant.id)
            return
        task = self._loops.get(tenant.id)
        if task is None or task.done():
            self._loops[tenant.id] = asyncio.create_task(self._pull_loop(tenant.id))
            return
        event = self._wake.get(tenant.id)
        if event is not None:
            event.set()

    def remove_tenant(self, tenant_id: int) -> None:
        task = self._loops.pop(tenant_id, None)
        self._wake.pop(tenant_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def align(self) -> None:
        """Align the running loops with the active tenants in storage."""
        async with self._repository_scope() as repo:
            tenants = await repo.list_active()
        wanted: set[int] = set()
        for tenant in tenants:
            if tenant.id is None:
                continue
            wanted.add(tenant.id)
            self.reschedule_tenant(tenant)
        for tenant_id in list(self._loops):
            if tenant_id not in wanted:
                self.remove_tenant(tenant_id)

    async def _safe_align(self) -> None:
        try:
            await self.align()
        except Exception as exc:  # a DB blip must not kill the alignment job
            logger.error("tenant_sync_align_failed", error=str(exc))

    def start(self) -> None:
        # next_run_time=now: start the per-tenant loops immediately at boot
        # instead of waiting a full alignment period.
        self._scheduler.add_job(
            self._safe_align,
            trigger=IntervalTrigger(minutes=self._align_interval_minutes),
            id=ALIGN_JOB_ID,
            replace_existing=True,
            next_run_time=datetime.now(UTC),
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        for tenant_id in list(self._loops):
            self.remove_tenant(tenant_id)
        self._scheduler.shutdown(wait=False)
