"""Tests for TenantSyncScheduler: per-tenant locking and inactive-tenant skipping."""
import asyncio

from bims_shopify.domain.tenant import Tenant
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
