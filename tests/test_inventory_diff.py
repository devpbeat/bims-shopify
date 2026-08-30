"""Tests for inventory diff computation used by SyncInventoryToShopify."""

from bims_shopify.application.sync_inventory import (
    NeedsConfirmation,
    diff_inventory,
)
from bims_shopify.domain.product import ProductSnapshot


def test_no_previous_stock_counts_as_full_delta():
    current = [ProductSnapshot(sku="SKU1", name="A", price=10.0, stock=5.0)]
    deltas = diff_inventory({}, current)
    assert len(deltas) == 1
    assert deltas[0].sku == "SKU1"
    assert deltas[0].previous_stock is None
    assert deltas[0].new_stock == 5.0
    assert deltas[0].delta == 5.0


def test_unchanged_stock_produces_no_delta():
    current = [ProductSnapshot(sku="SKU1", name="A", price=10.0, stock=5.0)]
    deltas = diff_inventory({"SKU1": 5.0}, current)
    assert deltas == []


def test_changed_stock_produces_delta_with_correct_magnitude():
    current = [ProductSnapshot(sku="SKU1", name="A", price=10.0, stock=3.0)]
    deltas = diff_inventory({"SKU1": 8.0}, current)
    assert len(deltas) == 1
    assert deltas[0].previous_stock == 8.0
    assert deltas[0].new_stock == 3.0
    assert deltas[0].delta == -5.0


def test_multiple_skus_only_changed_ones_reported():
    current = [
        ProductSnapshot(sku="SKU1", name="A", price=10.0, stock=5.0),
        ProductSnapshot(sku="SKU2", name="B", price=20.0, stock=2.0),
    ]
    deltas = diff_inventory({"SKU1": 5.0, "SKU2": 9.0}, current)
    assert len(deltas) == 1
    assert deltas[0].sku == "SKU2"


class _FakeERP:
    def __init__(self, products):
        self._products = products
        self.received_since = "not-called"

    async def list_products(self, tenant, since=None):
        self.received_since = since
        return self._products


class _FakeStorefront:
    def __init__(self):
        self.write_calls = 0

    async def find_variant_by_sku(self, tenant, sku):
        return {"inventoryItem": {"id": f"gid://{sku}"}}

    async def set_inventory_quantities(self, tenant, deltas):
        self.write_calls += 1

    async def upsert_product(self, tenant, sku, name, price):
        pass


async def test_dry_run_never_calls_storefront_writes(tenant):
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    erp = _FakeERP([ProductSnapshot(sku="SKU1", name="A", price=10.0, stock=5.0)])
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    report = await use_case.run(tenant, {}, dry_run=True)

    assert storefront.write_calls == 0
    assert report.total_products == 1
    assert report.would_update == 1
    assert report.sample_skus[0]["sku"] == "SKU1"


def test_snapshot_with_unresolved_stock_produces_no_delta():
    """A SKU absent from the stock response (stock=None) must never be
    pushed as a delta — it must be treated as 'unknown', not 'zero'."""
    current = [ProductSnapshot(sku="SKU1", name="A", price=10.0, stock=None)]
    deltas = diff_inventory({"SKU1": 5.0}, current)
    assert deltas == []


def test_snapshot_with_unresolved_stock_and_no_previous_produces_no_delta():
    current = [ProductSnapshot(sku="SKU1", name="A", price=10.0, stock=None)]
    deltas = diff_inventory({}, current)
    assert deltas == []


async def test_guard_aborts_push_when_zero_ratio_exceeds_threshold(tenant):
    """If pushing would zero out more than the configured percentage of
    matched variants, the run must abort BEFORE calling Shopify, even when
    dry_run=False, and report needs_confirmation."""
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    # 10 SKUs, all previously had stock, all now resolve to 0 -> 100% zero ratio.
    current = [
        ProductSnapshot(sku=f"SKU{i}", name="A", price=10.0, stock=0.0) for i in range(10)
    ]
    erp = _FakeERP(current)
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    result = await use_case.run(tenant, {f"SKU{i}": 5.0 for i in range(10)}, dry_run=False)

    assert isinstance(result, NeedsConfirmation)
    assert result.reason == "zero_ratio_exceeded"
    assert storefront.write_calls == 0


async def test_guard_aborts_push_when_changed_count_exceeds_max(tenant):
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [
        ProductSnapshot(sku=f"SKU{i}", name="A", price=10.0, stock=float(i + 1))
        for i in range(501)
    ]
    erp = _FakeERP(current)
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    result = await use_case.run(tenant, {}, dry_run=False)

    assert isinstance(result, NeedsConfirmation)
    assert result.reason == "max_changed_exceeded"
    assert storefront.write_calls == 0


async def test_guard_does_not_trigger_for_small_safe_changes(tenant):
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [ProductSnapshot(sku="SKU1", name="A", price=10.0, stock=3.0)]
    erp = _FakeERP(current)
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    result = await use_case.run(tenant, {"SKU1": 8.0}, dry_run=False)

    assert not isinstance(result, NeedsConfirmation)
    assert storefront.write_calls == 1


async def test_since_is_forwarded_to_erp(tenant):
    from datetime import UTC, datetime

    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    erp = _FakeERP([])
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    when = datetime(2026, 1, 1, tzinfo=UTC)
    await use_case.run(tenant, {}, since=when)

    assert erp.received_since == when
