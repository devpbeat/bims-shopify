"""Per-tenant locks: the pull and the push each serialise with themselves only,
the one-shot holds both."""
import asyncio
import contextlib

from bims_shopify.sync_locks import SyncLocks


async def test_pull_and_push_locks_are_independent():
    locks = SyncLocks()
    async with locks.pull(1):
        assert locks.pull(1).locked()
        assert not locks.push(1).locked()  # a slow pull never blocks the push
        async with locks.push(1):
            assert locks.push(1).locked()


async def test_locks_are_per_tenant():
    locks = SyncLocks()
    async with locks.pull(1):
        assert not locks.pull(2).locked()


async def test_combined_lock_reports_busy_when_either_worker_is_running():
    locks = SyncLocks()
    combined = locks.combined(1)
    assert not combined.locked()
    async with locks.push(1):
        assert combined.locked()
    async with locks.pull(1):
        assert combined.locked()


async def test_combined_lock_holds_both_and_releases_both():
    locks = SyncLocks()
    async with locks.combined(1):
        assert locks.pull(1).locked() and locks.push(1).locked()
    assert not locks.pull(1).locked() and not locks.push(1).locked()


async def test_combined_lock_waits_for_a_running_worker_instead_of_overlapping():
    locks = SyncLocks()
    order: list[str] = []

    async def scheduled_pull():
        async with locks.pull(1):
            order.append("pull-start")
            await asyncio.sleep(0.05)
            order.append("pull-end")

    async def one_shot():
        async with locks.combined(1):
            order.append("oneshot")

    task = asyncio.create_task(scheduled_pull())
    await asyncio.sleep(0.01)
    await one_shot()
    await task
    assert order == ["pull-start", "pull-end", "oneshot"]


async def test_combined_lock_releases_the_first_lock_if_the_second_cannot_be_taken():
    locks = SyncLocks()
    combined = locks.combined(1)
    await locks.push(1).acquire()
    waiter = asyncio.create_task(combined.__aenter__())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter
    locks.push(1).release()
    assert not locks.pull(1).locked()  # the pull lock taken first was given back
