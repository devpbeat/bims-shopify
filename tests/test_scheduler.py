"""Tests for TenantSyncScheduler: two independent workers per tenant (pull and
push), each with its own interval, its own non-overlap lock and live rescheduling."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from bims_shopify.domain.tenant import SyncMode, Tenant
from bims_shopify.scheduler import TenantSyncScheduler


def _make_tenant(tenant_id: int, active: bool = True) -> Tenant:
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


async def test_inactive_tenants_are_never_returned_by_list_active():
    repo = FakeTenantRepository([_make_tenant(1, active=True), _make_tenant(2, active=False)])
    calls: list[int] = []

    async def sync_fn(tenant: Tenant) -> None:
        calls.append(tenant.id)

    scheduler = TenantSyncScheduler(repo, sync_fn)
    await scheduler.run_once()
    await asyncio.sleep(0.05)
    assert calls == [1]


async def test_overlapping_runs_for_same_tenant_are_skipped():
    repo = FakeTenantRepository([_make_tenant(1, active=True)])
    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def sync_fn(tenant: Tenant) -> None:
        nonlocal call_count
        call_count += 1
        started.set()
        await release.wait()

    scheduler = TenantSyncScheduler(repo, sync_fn)
    await scheduler.run_once()
    await started.wait()

    # Second tick while the first run is still in flight should be skipped.
    await scheduler.run_once()
    await asyncio.sleep(0.05)
    assert call_count == 1

    release.set()
    await asyncio.sleep(0.05)


def _job_interval(
    scheduler: TenantSyncScheduler, tenant_id: int, kind: str = "pull"
) -> timedelta | None:
    job = scheduler._scheduler.get_job(f"tenant-{kind}-{tenant_id}")
    return job.trigger.interval if job is not None else None


async def test_each_tenant_gets_its_own_job_with_its_own_interval():
    repo = FakeTenantRepository(
        [
            replace(_make_tenant(1), sync_interval_minutes=15),
            replace(_make_tenant(2), sync_interval_minutes=45),
        ]
    )
    scheduler = TenantSyncScheduler(repo, lambda t: None)
    scheduler.start()
    try:
        await scheduler.reconcile()
        assert _job_interval(scheduler, 1) == timedelta(minutes=15)
        assert _job_interval(scheduler, 2) == timedelta(minutes=45)
    finally:
        scheduler.shutdown()


async def test_interval_change_reschedules_without_restart():
    tenant = replace(_make_tenant(1), sync_interval_minutes=30)
    repo = FakeTenantRepository([tenant])
    scheduler = TenantSyncScheduler(repo, lambda t: None)
    scheduler.start()
    try:
        await scheduler.reconcile()
        assert _job_interval(scheduler, 1) == timedelta(minutes=30)

        # Picked up from the stored row by the periodic reconcile...
        repo.set(replace(tenant, sync_interval_minutes=20))
        await scheduler.reconcile()
        assert _job_interval(scheduler, 1) == timedelta(minutes=20)

        # ...and immediately when the tenant API pushes the saved tenant.
        saved = replace(tenant, sync_interval_minutes=12)
        repo.set(saved)
        scheduler.reschedule_tenant(saved)
        assert _job_interval(scheduler, 1) == timedelta(minutes=12)
    finally:
        scheduler.shutdown()


async def test_reconcile_leaves_unchanged_jobs_alone():
    repo = FakeTenantRepository([_make_tenant(1)])
    scheduler = TenantSyncScheduler(repo, lambda t: None, push_fn=lambda t: None)
    scheduler.start()
    try:
        await scheduler.reconcile()
        before = {k: scheduler._scheduler.get_job(f"tenant-{k}-1").next_run_time for k in ("pull", "push")}
        await scheduler.reconcile()
        after = {k: scheduler._scheduler.get_job(f"tenant-{k}-1").next_run_time for k in ("pull", "push")}
        assert after == before
    finally:
        scheduler.shutdown()


async def test_deactivated_tenant_loses_its_job():
    tenant = _make_tenant(1)
    repo = FakeTenantRepository([tenant])
    scheduler = TenantSyncScheduler(repo, lambda t: None)
    scheduler.start()
    try:
        await scheduler.reconcile()
        assert _job_interval(scheduler, 1) is not None
        repo.set(replace(tenant, active=False))
        await scheduler.reconcile()
        assert _job_interval(scheduler, 1) is None
    finally:
        scheduler.shutdown()


async def test_tick_syncs_the_freshly_stored_tenant_not_a_startup_snapshot():
    tenant = _make_tenant(1)
    repo = FakeTenantRepository([tenant])
    seen: list[str] = []

    async def sync_fn(t: Tenant) -> None:
        seen.append(t.sync_mode.value)

    scheduler = TenantSyncScheduler(repo, sync_fn)
    await scheduler.reconcile()
    repo.set(replace(tenant, sync_mode=SyncMode.INCREMENTAL))
    await scheduler.run_tenant(1)
    await asyncio.sleep(0.05)
    assert seen == ["incremental"]


# -- two workers per tenant -------------------------------------------------------


async def _noop(tenant: Tenant) -> None:
    return None


async def test_each_tenant_gets_a_pull_job_and_a_push_job_with_their_own_intervals():
    tenant = replace(_make_tenant(1), sync_interval_minutes=30, push_interval_minutes=5)
    scheduler = TenantSyncScheduler(FakeTenantRepository([tenant]), _noop, push_fn=_noop)
    scheduler.start()
    try:
        await scheduler.reconcile()
        assert _job_interval(scheduler, 1, "pull") == timedelta(minutes=30)
        assert _job_interval(scheduler, 1, "push") == timedelta(minutes=5)
    finally:
        scheduler.shutdown()


async def test_push_interval_reschedules_live_without_touching_the_pull_job():
    tenant = replace(_make_tenant(1), sync_interval_minutes=30, push_interval_minutes=5)
    repo = FakeTenantRepository([tenant])
    scheduler = TenantSyncScheduler(repo, _noop, push_fn=_noop)
    scheduler.start()
    try:
        await scheduler.reconcile()
        pull_next = scheduler._scheduler.get_job("tenant-pull-1").next_run_time

        # picked up from storage by the periodic reconcile...
        repo.set(replace(tenant, push_interval_minutes=2))
        await scheduler.reconcile()
        assert _job_interval(scheduler, 1, "push") == timedelta(minutes=2)

        # ...and immediately when the tenant API pushes the saved tenant.
        saved = replace(tenant, push_interval_minutes=1)
        repo.set(saved)
        scheduler.reschedule_tenant(saved)
        assert _job_interval(scheduler, 1, "push") == timedelta(minutes=1)

        assert _job_interval(scheduler, 1, "pull") == timedelta(minutes=30)
        assert scheduler._scheduler.get_job("tenant-pull-1").next_run_time == pull_next
    finally:
        scheduler.shutdown()


async def test_pull_interval_reschedules_live_without_touching_the_push_job():
    tenant = replace(_make_tenant(1), sync_interval_minutes=30, push_interval_minutes=5)
    repo = FakeTenantRepository([tenant])
    scheduler = TenantSyncScheduler(repo, _noop, push_fn=_noop)
    scheduler.start()
    try:
        await scheduler.reconcile()
        push_next = scheduler._scheduler.get_job("tenant-push-1").next_run_time
        saved = replace(tenant, sync_interval_minutes=45)
        repo.set(saved)
        scheduler.reschedule_tenant(saved)
        assert _job_interval(scheduler, 1, "pull") == timedelta(minutes=45)
        assert scheduler._scheduler.get_job("tenant-push-1").next_run_time == push_next
    finally:
        scheduler.shutdown()


async def test_deactivated_tenant_loses_both_jobs():
    tenant = _make_tenant(1)
    repo = FakeTenantRepository([tenant])
    scheduler = TenantSyncScheduler(repo, _noop, push_fn=_noop)
    scheduler.start()
    try:
        await scheduler.reconcile()
        repo.set(replace(tenant, active=False))
        await scheduler.reconcile()
        assert _job_interval(scheduler, 1, "pull") is None
        assert _job_interval(scheduler, 1, "push") is None
    finally:
        scheduler.shutdown()


async def test_a_pull_tick_that_finds_the_previous_pull_running_is_skipped_and_recorded():
    repo = FakeTenantRepository([_make_tenant(1)])
    started, release = asyncio.Event(), asyncio.Event()
    runs, skips = [], []

    async def slow_pull(tenant: Tenant) -> None:
        runs.append(tenant.id)
        started.set()
        await release.wait()

    async def on_skip(kind: str, tenant: Tenant) -> None:
        skips.append((kind, tenant.id))

    scheduler = TenantSyncScheduler(repo, slow_pull, push_fn=_noop, skip_fn=on_skip)
    await scheduler.run_tenant(1)
    await started.wait()

    await scheduler.run_tenant(1)  # next interval fires while the pull is still going
    await scheduler.run_tenant(1)
    assert runs == [1]  # never queued, never overlapped
    assert skips == [("pull", 1), ("pull", 1)]

    release.set()
    await asyncio.sleep(0.02)
    await scheduler.run_tenant(1)  # free again: runs normally
    await asyncio.sleep(0.02)
    assert runs == [1, 1]


async def test_two_simultaneous_pull_ticks_start_only_one_pull():
    repo = FakeTenantRepository([_make_tenant(1)])
    release = asyncio.Event()
    runs = []

    async def slow_pull(tenant: Tenant) -> None:
        runs.append(tenant.id)
        await release.wait()

    scheduler = TenantSyncScheduler(repo, slow_pull, push_fn=_noop)
    await asyncio.gather(scheduler.run_tenant(1), scheduler.run_tenant(1))
    await asyncio.sleep(0.02)
    assert runs == [1]
    release.set()
    await asyncio.sleep(0.02)


async def test_a_slow_pull_never_blocks_the_push():
    repo = FakeTenantRepository([_make_tenant(1)])
    started, release = asyncio.Event(), asyncio.Event()
    pushes, skips = [], []

    async def slow_pull(tenant: Tenant) -> None:
        started.set()
        await release.wait()

    async def push(tenant: Tenant) -> None:
        pushes.append(tenant.id)

    async def on_skip(kind: str, tenant: Tenant) -> None:
        skips.append(kind)

    scheduler = TenantSyncScheduler(repo, slow_pull, push_fn=push, skip_fn=on_skip)
    await scheduler.run_tenant(1)
    await started.wait()

    for _ in range(3):  # three push ticks while BIMS is still being pulled
        await scheduler.run_push_tenant(1)
        await asyncio.sleep(0.02)
    assert pushes == [1, 1, 1]
    assert skips == []
    release.set()
    await asyncio.sleep(0.02)


async def test_a_push_tick_that_finds_the_previous_push_running_is_skipped_and_recorded():
    repo = FakeTenantRepository([_make_tenant(1)])
    started, release = asyncio.Event(), asyncio.Event()
    pushes, skips = [], []

    async def slow_push(tenant: Tenant) -> None:
        pushes.append(tenant.id)
        started.set()
        await release.wait()

    async def on_skip(kind: str, tenant: Tenant) -> None:
        skips.append(kind)

    scheduler = TenantSyncScheduler(repo, _noop, push_fn=slow_push, skip_fn=on_skip)
    await scheduler.run_push_tenant(1)
    await started.wait()
    await scheduler.run_push_tenant(1)
    assert pushes == [1] and skips == ["push"]
    release.set()
    await asyncio.sleep(0.02)


async def test_the_shared_lock_makes_scheduled_ticks_stand_aside_for_a_one_shot():
    repo = FakeTenantRepository([_make_tenant(1)])
    runs, skips = [], []

    async def work(tenant: Tenant) -> None:
        runs.append(tenant.id)

    async def on_skip(kind: str, tenant: Tenant) -> None:
        skips.append(kind)

    scheduler = TenantSyncScheduler(repo, work, push_fn=work, skip_fn=on_skip)
    async with scheduler.lock_for_tenant(1):  # an operator's pull-now-then-push-now
        await scheduler.run_tenant(1)
        await scheduler.run_push_tenant(1)
    assert runs == [] and sorted(skips) == ["pull", "push"]


async def test_a_failing_skip_recorder_does_not_break_the_tick():
    repo = FakeTenantRepository([_make_tenant(1)])
    release = asyncio.Event()

    async def slow(tenant: Tenant) -> None:
        await release.wait()

    async def broken_skip(kind: str, tenant: Tenant) -> None:
        raise RuntimeError("db blip")

    scheduler = TenantSyncScheduler(repo, slow, push_fn=_noop, skip_fn=broken_skip)
    await scheduler.run_tenant(1)
    await scheduler.run_tenant(1)  # skip recording explodes; the tick must survive
    release.set()
    await asyncio.sleep(0.02)


async def test_first_pull_runs_at_boot_when_there_is_no_snapshot_history():
    tenant = replace(_make_tenant(1), sync_interval_minutes=30)

    async def never_pulled(t: Tenant):
        return None

    scheduler = TenantSyncScheduler(
        FakeTenantRepository([tenant]), _noop, push_fn=_noop, last_pull_fn=never_pulled
    )
    scheduler.start()
    try:
        await scheduler.reconcile()
        next_pull = scheduler._scheduler.get_job("tenant-pull-1").next_run_time
        assert next_pull - datetime.now(UTC) < timedelta(seconds=5)
    finally:
        scheduler.shutdown()


async def test_a_restart_does_not_repull_a_fresh_snapshot_nor_starve_an_old_one():
    fresh = replace(_make_tenant(1), sync_interval_minutes=30)
    old = replace(_make_tenant(2), sync_interval_minutes=30)
    now = datetime.now(UTC)
    last = {1: now - timedelta(minutes=5), 2: now - timedelta(hours=3)}

    async def last_pull(t: Tenant):
        return last[t.id]

    scheduler = TenantSyncScheduler(
        FakeTenantRepository([fresh, old]), _noop, push_fn=_noop, last_pull_fn=last_pull
    )
    scheduler.start()
    try:
        await scheduler.reconcile()
        fresh_next = scheduler._scheduler.get_job("tenant-pull-1").next_run_time
        old_next = scheduler._scheduler.get_job("tenant-pull-2").next_run_time
        assert timedelta(minutes=24) < fresh_next - now < timedelta(minutes=26)  # 30 - 5
        assert old_next - datetime.now(UTC) < timedelta(seconds=5)  # overdue: now
    finally:
        scheduler.shutdown()
