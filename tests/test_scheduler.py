"""Tests for TenantSyncScheduler: per-tenant locking and inactive-tenant skipping."""
import asyncio
from dataclasses import replace
from datetime import timedelta

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


def _job_interval(scheduler: TenantSyncScheduler, tenant_id: int) -> timedelta | None:
    job = scheduler._scheduler.get_job(f"tenant-sync-{tenant_id}")
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
    scheduler = TenantSyncScheduler(repo, lambda t: None)
    scheduler.start()
    try:
        await scheduler.reconcile()
        before = scheduler._scheduler.get_job("tenant-sync-1").next_run_time
        await scheduler.reconcile()
        assert scheduler._scheduler.get_job("tenant-sync-1").next_run_time == before
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
