"""AsyncIOScheduler that periodically syncs every active tenant.

A per-tenant asyncio.Lock prevents overlapping runs for the same tenant if a
previous run has not finished by the time the next tick fires.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bims_shopify.domain.tenant import Tenant
from bims_shopify.ports.repositories import TenantRepository

TenantSyncFn = Callable[[Tenant], Awaitable[None]]


class TenantSyncScheduler:
    """Iterates active tenants on an interval, skipping inactive ones and
    skipping tenants whose previous run is still in flight."""

    def __init__(
        self,
        tenant_repository: TenantRepository,
        sync_fn: TenantSyncFn,
        interval_minutes: int = 30,
    ) -> None:
        self._tenant_repository = tenant_repository
        self._sync_fn = sync_fn
        self._interval_minutes = interval_minutes
        self._locks: dict[int, asyncio.Lock] = {}
        self._scheduler = AsyncIOScheduler()
        # asyncio only holds a weak reference to a task once it is created;
        # without keeping our own strong reference here it can be garbage
        # collected mid-run. Discard from the set once the task is done.
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _lock_for(self, tenant_id: int) -> asyncio.Lock:
        if tenant_id not in self._locks:
            self._locks[tenant_id] = asyncio.Lock()
        return self._locks[tenant_id]

    def lock_for_tenant(self, tenant_id: int) -> asyncio.Lock:
        """Public accessor so other entry points (e.g. the manual run
        endpoint) can share this scheduler's per-tenant lock and avoid
        running concurrently with a scheduled sync for the same tenant."""
        return self._lock_for(tenant_id)

    async def run_once(self) -> None:
        tenants = await self._tenant_repository.list_active()
        for tenant in tenants:
            if not tenant.active or tenant.id is None:
                continue
            lock = self._lock_for(tenant.id)
            if lock.locked():
                continue
            task = asyncio.create_task(self._run_with_lock(lock, tenant))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _run_with_lock(self, lock: asyncio.Lock, tenant: Tenant) -> None:
        async with lock:
            await self._sync_fn(tenant)

    def start(self) -> None:
        self._scheduler.add_job(
            self.run_once,
            trigger=IntervalTrigger(minutes=self._interval_minutes),
            id="tenant-sync",
            replace_existing=True,
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
