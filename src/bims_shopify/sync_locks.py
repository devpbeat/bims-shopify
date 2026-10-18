"""Per-tenant locks for the inventory-sync workers.

The pull and the write cycle are independent on purpose: a pull that takes ten
minutes must never be queued behind a write cycle, and the write cycle must never be
queued behind the next pull. Each worker serialises only with itself (so a worker never
overlaps itself), and the one-shot ``sync`` (pull now, then write now) takes both,
always pull first, so it can neither overlap nor deadlock with the scheduled workers.

The WRITE lock is the one that guards every write of quantities and catalog to Shopify:
the scheduled write cycle, the manual one-shot and the on-demand ``reconcile`` ops
command all hold it, so they never overlap. A pull only lands a new snapshot, and a write
keeps using the one it started from, so the pull lock is not needed for a write alone.
"""
from __future__ import annotations

import asyncio


class CombinedLock:
    """Async context manager holding several locks, acquired in order and
    released in reverse. ``locked()`` is true while any of them is held, which
    is what the HTTP endpoint checks to answer 409."""

    def __init__(self, *locks: asyncio.Lock) -> None:
        self._locks = locks

    def locked(self) -> bool:
        return any(lock.locked() for lock in self._locks)

    async def __aenter__(self) -> CombinedLock:
        acquired: list[asyncio.Lock] = []
        try:
            for lock in self._locks:
                await lock.acquire()
                acquired.append(lock)
        except BaseException:
            for lock in reversed(acquired):
                lock.release()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        for lock in reversed(self._locks):
            lock.release()


class SyncLocks:
    def __init__(self) -> None:
        self._pull: dict[int, asyncio.Lock] = {}
        self._write: dict[int, asyncio.Lock] = {}

    def pull(self, tenant_id: int) -> asyncio.Lock:
        return self._pull.setdefault(tenant_id, asyncio.Lock())

    def write(self, tenant_id: int) -> asyncio.Lock:
        return self._write.setdefault(tenant_id, asyncio.Lock())

    def combined(self, tenant_id: int) -> CombinedLock:
        """Both workers' locks (pull first): held by the one-shot sync."""
        return CombinedLock(self.pull(tenant_id), self.write(tenant_id))
