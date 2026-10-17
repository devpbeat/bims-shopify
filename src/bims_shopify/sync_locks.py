"""Per-tenant locks for the inventory-sync workers.

The pull and the push are independent on purpose: a pull that takes sixteen
minutes must never stop the push from running every few minutes. Each worker
serialises only with itself (so a worker never overlaps itself), and the
one-shot ``sync`` (pull now, then push now) takes both, always pull first, so
it can neither overlap nor deadlock with the scheduled workers.

The reconcile (Shopify's real quantities vs the snapshot) is a third worker
with its own lock, so it never overlaps itself, and it also takes the PUSH
lock, always after its own: both write quantities to Shopify, and a push
running beside a reconcile could overwrite its values from an older read. It
does not take the pull lock: a pull only lands a new snapshot, and the
reconcile keeps using the one it started from.
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
        self._push: dict[int, asyncio.Lock] = {}
        self._reconcile: dict[int, asyncio.Lock] = {}

    def pull(self, tenant_id: int) -> asyncio.Lock:
        return self._pull.setdefault(tenant_id, asyncio.Lock())

    def push(self, tenant_id: int) -> asyncio.Lock:
        return self._push.setdefault(tenant_id, asyncio.Lock())

    def reconcile(self, tenant_id: int) -> asyncio.Lock:
        return self._reconcile.setdefault(tenant_id, asyncio.Lock())

    def reconcile_worker(self, tenant_id: int) -> CombinedLock:
        """The reconcile lock, then the push lock: held by an on-demand reconcile."""
        return CombinedLock(self.reconcile(tenant_id), self.push(tenant_id))

    def combined(self, tenant_id: int) -> CombinedLock:
        """Both workers' locks (pull first): held by the one-shot sync."""
        return CombinedLock(self.pull(tenant_id), self.push(tenant_id))
