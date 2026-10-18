"""TenantSyncScheduler: a continuous pull loop per tenant, and one write cycle per new snapshot.

Time is virtual (a fake clock and sleep), so a ten-minute pull and a sixty-second gap cost
nothing and every assertion is exact.
"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from bims_shopify.domain.tenant import Tenant
from bims_shopify.scheduler import TenantSyncScheduler


def _make_tenant(tenant_id: int, active: bool = True, **overrides) -> Tenant:
    return Tenant(
        id=tenant_id,
        slug=f"tenant-{tenant_id}",
        bims_base_url="https://bims.example.com",
        bims_api_key="key",
        shopify_shop_domain="shop.myshopify.com",
        shopify_access_token="token",
        shopify_webhook_secret="secret",
        shopify_location_id="loc",
        bims_posale_id=1,
        bims_warehouse_id=1,
        bims_company_id=1,
        bims_currency_id=1,
        bims_payment_method_id=1,
        default_customer_contact_id=1,
        active=active,
        **overrides,
    )


class FakeTenantRepository:
    def __init__(self, tenants: list[Tenant]):
        self._tenants = tenants

    async def list_active(self) -> list[Tenant]:
        return [t for t in self._tenants if t.active]

    async def get_by_id(self, tenant_id: int) -> Tenant | None:
        return next((t for t in self._tenants if t.id == tenant_id), None)

    def set(self, tenant: Tenant) -> None:
        """Simulate the tenant API saving a change to the stored row."""
        self._tenants = [tenant if t.id == tenant.id else t for t in self._tenants]


class VirtualTime:
    """A clock the loop's sleeps advance, so waiting costs no real time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)

    async def work(self, seconds: float) -> None:
        """Time passing because a worker is busy (a pull answering slowly)."""
        self.now += seconds
        await asyncio.sleep(0)


def _scheduler(repo, pull_fn, time: VirtualTime, **kwargs) -> TenantSyncScheduler:
    return TenantSyncScheduler(
        repo, pull_fn, clock=time.clock, sleep=time.sleep, **kwargs
    )


async def _until(condition, *, spins: int = 2000) -> None:
    for _ in range(spins):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _settle(spins: int = 50) -> None:
    for _ in range(spins):
        await asyncio.sleep(0)


# -- pulls run back to back, paced by their own duration --------------------------


async def test_pulls_run_back_to_back_separated_only_by_the_minimum_gap():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=60)])
    starts: list[float] = []

    async def pull(tenant: Tenant) -> bool:
        starts.append(time.now)
        return True  # answers instantly

    scheduler = _scheduler(repo, pull, time)
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: len(starts) >= 4)
    scheduler.remove_tenant(1)

    gaps = [b - a for a, b in pairwise(starts)]
    assert gaps[:3] == [60, 60, 60]  # never a hot loop, never a fixed cadence beyond the gap


async def test_a_slow_pull_is_followed_by_the_gap_not_by_a_fixed_interval():
    """There is no 30-minute tick: a ten-minute pull is followed by the next one a gap later."""
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=60)])
    starts: list[float] = []

    async def pull(tenant: Tenant) -> bool:
        starts.append(time.now)
        await time.work(600)
        return True

    scheduler = _scheduler(repo, pull, time)
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: len(starts) >= 3)
    scheduler.remove_tenant(1)

    assert [b - a for a, b in pairwise(starts)][:2] == [660, 660]


async def test_a_pull_never_overlaps_itself():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=10)])
    running = 0
    peak = 0
    done = 0

    async def pull(tenant: Tenant) -> bool:
        nonlocal running, peak, done
        running += 1
        peak = max(peak, running)
        await time.work(5)
        await asyncio.sleep(0)
        running -= 1
        done += 1
        return True

    scheduler = _scheduler(repo, pull, time)
    # Poking the loop (a settings change) and re-aligning while a pull runs must not start
    # a second loop or a second pull.
    scheduler.reschedule_tenant(repo._tenants[0])
    for _ in range(5):
        scheduler.reschedule_tenant(repo._tenants[0])
        await scheduler.align()
        await asyncio.sleep(0)
    await _until(lambda: done >= 5)
    scheduler.remove_tenant(1)
    assert peak == 1


async def test_a_manual_sync_holding_the_pull_lock_makes_the_loop_stand_aside_once():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=30)])
    pulls: list[float] = []
    skips: list[str] = []

    async def pull(tenant: Tenant) -> bool:
        pulls.append(time.now)
        return False

    async def skip(kind: str, tenant: Tenant) -> None:
        skips.append(kind)

    scheduler = _scheduler(repo, pull, time, skip_fn=skip)
    async with scheduler.lock_for_tenant(1):  # the one-shot sync owns both locks
        scheduler.reschedule_tenant(repo._tenants[0])
        await _settle()
        assert pulls == []
        assert skips == ["pull"]  # recorded ONCE for the streak, not once per gap
    await _until(lambda: len(pulls) >= 1)
    scheduler.remove_tenant(1)


async def test_a_failing_pull_does_not_stop_the_loop_and_still_respects_the_gap():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=60)])
    starts: list[float] = []

    async def pull(tenant: Tenant) -> bool:
        starts.append(time.now)
        raise RuntimeError("BIMS answered 500")

    scheduler = _scheduler(repo, pull, time)
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: len(starts) >= 3)
    scheduler.remove_tenant(1)
    assert [b - a for a, b in pairwise(starts)][:2] == [60, 60]


async def test_the_gap_is_live_editable_and_applies_to_the_next_wait():
    time = VirtualTime()
    tenant = _make_tenant(1, pull_min_gap_seconds=600)
    repo = FakeTenantRepository([tenant])
    starts: list[float] = []

    async def pull(t: Tenant) -> bool:
        starts.append(time.now)
        return True

    scheduler = _scheduler(repo, pull, time)
    scheduler.reschedule_tenant(tenant)
    await _until(lambda: len(starts) >= 1)
    await _settle(5)  # the loop is now waiting out a 600s gap
    repo.set(replace(tenant, pull_min_gap_seconds=60))
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: len(starts) >= 3)
    scheduler.remove_tenant(1)
    # The wait in progress honoured the new, shorter gap instead of finishing the old one.
    assert starts[1] - starts[0] < 600 and starts[2] - starts[1] == 60


async def test_first_pull_after_a_restart_waits_out_the_gap_since_the_last_pull():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=60)])
    starts: list[float] = []

    async def pull(tenant: Tenant) -> bool:
        starts.append(time.now)
        return True

    async def last_pull(tenant: Tenant):
        return datetime.now(UTC) - timedelta(seconds=20)  # a pull finished 20s ago

    scheduler = _scheduler(repo, pull, time, last_pull_fn=last_pull)
    t0 = time.now
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: len(starts) >= 1)
    scheduler.remove_tenant(1)
    assert 39 <= starts[0] - t0 <= 41  # 60s gap - 20s already elapsed


async def test_a_tenant_never_pulled_pulls_at_once():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1)])
    starts: list[float] = []

    async def pull(tenant: Tenant) -> bool:
        starts.append(time.now)
        return True

    async def never(tenant: Tenant):
        return None

    scheduler = _scheduler(repo, pull, time, last_pull_fn=never)
    t0 = time.now
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: len(starts) >= 1)
    scheduler.remove_tenant(1)
    assert starts[0] == t0


# -- one write cycle per new snapshot -------------------------------------------------


async def test_a_write_cycle_runs_once_per_new_snapshot_and_not_for_a_pull_that_landed_nothing():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=10)])
    outcomes = iter([True, False, True, False, False, True])  # rejected / failed pulls: False
    pulls = 0
    cycles: list[int] = []

    async def pull(tenant: Tenant) -> bool:
        nonlocal pulls
        pulls += 1
        return next(outcomes, False)

    async def cycle(tenant: Tenant) -> None:
        cycles.append(pulls)

    scheduler = _scheduler(repo, pull, time, cycle_fn=cycle)
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: pulls >= 8)
    await _settle()
    scheduler.remove_tenant(1)
    assert cycles == [1, 3, 6]  # exactly the pulls that landed a snapshot


async def test_with_no_new_snapshot_no_write_cycle_ever_runs():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=10)])
    pulls = 0
    cycles = 0

    async def pull(tenant: Tenant) -> bool:
        nonlocal pulls
        pulls += 1
        return False

    async def cycle(tenant: Tenant) -> None:
        nonlocal cycles
        cycles += 1

    scheduler = _scheduler(repo, pull, time, cycle_fn=cycle)
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: pulls >= 5)
    await _settle()
    scheduler.remove_tenant(1)
    assert cycles == 0


async def test_a_cycle_that_outlasts_the_next_snapshot_skips_it_instead_of_stacking():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=10)])
    release = asyncio.Event()
    cycles_started = 0
    running = 0
    peak = 0
    pulls = 0
    skips: list[str] = []

    async def pull(tenant: Tenant) -> bool:
        nonlocal pulls
        pulls += 1
        await time.work(5)
        return True

    async def cycle(tenant: Tenant) -> None:
        nonlocal cycles_started, running, peak
        cycles_started += 1
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    async def skip(kind: str, tenant: Tenant) -> None:
        skips.append(kind)

    scheduler = _scheduler(repo, pull, time, cycle_fn=cycle, skip_fn=skip)
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: len(skips) >= 4)  # snapshots land while the first cycle is still reading

    assert cycles_started == 1 and peak == 1  # nothing queued, nothing overlapping
    assert set(skips) == {"cycle"}  # every snapshot that found it busy was recorded as skipped
    assert pulls >= 5  # and the pull loop was never held up by the cycle

    release.set()
    await _until(lambda: running == 0)
    await _until(lambda: cycles_started >= 2)  # the next snapshot after the cycle gets its own
    release.set()
    scheduler.remove_tenant(1)
    assert peak == 1


async def test_an_on_demand_write_holding_the_write_lock_makes_the_cycle_skip():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=10)])
    cycles = 0
    skips: list[str] = []
    pulls = 0

    async def pull(tenant: Tenant) -> bool:
        nonlocal pulls
        pulls += 1
        return True

    async def cycle(tenant: Tenant) -> None:
        nonlocal cycles
        cycles += 1

    async def skip(kind: str, tenant: Tenant) -> None:
        skips.append(kind)

    scheduler = _scheduler(repo, pull, time, cycle_fn=cycle, skip_fn=skip)
    async with scheduler.write_lock_for_tenant(1):  # e.g. the "reconcile" ops command
        scheduler.reschedule_tenant(repo._tenants[0])
        await _until(lambda: pulls >= 3)
        await _settle()
        assert cycles == 0 and skips[:3] == ["cycle"] * 3
    await _until(lambda: cycles >= 1)
    scheduler.remove_tenant(1)


async def test_a_cycle_that_raises_releases_the_write_lock_so_the_next_snapshot_runs():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=10)])
    cycles = 0

    async def pull(tenant: Tenant) -> bool:
        return True

    async def cycle(tenant: Tenant) -> None:
        nonlocal cycles
        cycles += 1
        raise RuntimeError("boom")

    scheduler = _scheduler(repo, pull, time, cycle_fn=cycle)
    scheduler.reschedule_tenant(repo._tenants[0])
    await _until(lambda: cycles >= 3)  # each failed cycle let the next snapshot start a new one
    scheduler.remove_tenant(1)
    await _settle()
    assert not scheduler.locks.write(1).locked()


async def test_the_cycle_is_per_tenant_one_slow_tenant_does_not_skip_another():
    time = VirtualTime()
    repo = FakeTenantRepository(
        [_make_tenant(1, pull_min_gap_seconds=10), _make_tenant(2, pull_min_gap_seconds=10)]
    )
    release = asyncio.Event()
    cycles: dict[int, int] = {1: 0, 2: 0}

    async def pull(tenant: Tenant) -> bool:
        return True

    async def cycle(tenant: Tenant) -> None:
        cycles[tenant.id] += 1
        if tenant.id == 1:
            await release.wait()

    scheduler = _scheduler(repo, pull, time, cycle_fn=cycle)
    await scheduler.align()
    await _until(lambda: cycles[2] >= 3)
    assert cycles[1] == 1
    release.set()
    scheduler.remove_tenant(1)
    scheduler.remove_tenant(2)


# -- loop management --------------------------------------------------------------


async def test_inactive_tenants_get_no_loop_and_a_deactivated_one_stops():
    time = VirtualTime()
    active = _make_tenant(1, pull_min_gap_seconds=10)
    repo = FakeTenantRepository([active, _make_tenant(2, active=False)])
    pulled: set[int] = set()

    async def pull(tenant: Tenant) -> bool:
        pulled.add(tenant.id)
        return True

    scheduler = _scheduler(repo, pull, time)
    await scheduler.align()
    await _until(lambda: 1 in pulled)
    assert set(scheduler._loops) == {1} and pulled == {1}

    repo.set(replace(active, active=False))  # deactivated through the API
    await _until(lambda: not scheduler._loops or scheduler._loops[1].done())
    await scheduler.align()
    assert scheduler._loops == {}


async def test_align_drops_the_loop_of_a_tenant_that_no_longer_exists():
    time = VirtualTime()
    repo = FakeTenantRepository([_make_tenant(1, pull_min_gap_seconds=10)])

    async def pull(tenant: Tenant) -> bool:
        return True

    scheduler = _scheduler(repo, pull, time)
    await scheduler.align()
    assert set(scheduler._loops) == {1}
    repo._tenants = []
    await scheduler.align()
    assert scheduler._loops == {}
