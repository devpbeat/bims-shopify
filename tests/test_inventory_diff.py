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


class _AlwaysResolvingSkuMap(dict):
    """Stand-in for a bulk sku->inventory_item_id map that resolves any SKU.

    Mirrors the old _FakeStorefront.find_variant_by_sku behavior (which
    always returned a variant) without needing to know every SKU a test
    will ask about ahead of time.
    """

    def get(self, key, default=None):
        return f"gid://{key}"

    def __contains__(self, key):
        return True


class _FakeStorefront:
    def __init__(self):
        self.write_calls = 0

    async def build_sku_inventory_map(self, tenant):
        return _AlwaysResolvingSkuMap()

    async def find_variant_by_sku(self, tenant, sku):
        return {"inventoryItem": {"id": f"gid://{sku}"}}

    async def set_inventory_quantities(self, tenant, deltas):
        self.write_calls += 1

    async def upsert_product(self, tenant, sku, name, price):
        pass


class _FakeStorefrontBulkMap:
    """Storefront fake whose bulk map may include SKUs that a search-based
    find_variant_by_sku lookup would miss (e.g. drafts). find_variant_by_sku
    always returns None here to prove the sync no longer depends on it."""

    def __init__(self, sku_map):
        self.sku_map = sku_map
        self.write_calls = 0
        self.pushed_deltas = []
        self.search_called = False

    async def build_sku_inventory_map(self, tenant):
        return self.sku_map

    async def find_variant_by_sku(self, tenant, sku):
        self.search_called = True
        return None

    async def set_inventory_quantities(self, tenant, deltas):
        self.write_calls += 1
        self.pushed_deltas.extend(deltas)

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


async def test_force_pushes_all_matched_variants_even_when_hashes_match(tenant):
    """Without force, unchanged stock vs. the stored watermark produces no
    deltas. With force=True, previous_stocks is ignored for diffing purposes
    (the caller passes {} in production, but here we exercise the use case
    directly with a non-empty previous_stocks equal to current stock to
    prove force bypasses the diff-based skip, not just an empty dict)."""
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [
        ProductSnapshot(sku=f"SKU{i}", name="A", price=10.0, stock=float(i))
        for i in range(5)
    ]
    previous_stocks = {f"SKU{i}": float(i) for i in range(5)}
    erp = _FakeERP(current)
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    non_force_result = await use_case.run(tenant, previous_stocks, dry_run=False)
    assert non_force_result == []
    assert storefront.write_calls == 0

    force_result = await use_case.run(tenant, {}, dry_run=False, force=True)
    assert len(force_result) == 5
    assert storefront.write_calls == 1


async def test_force_bypasses_safety_guard_when_it_would_otherwise_trip(tenant):
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [
        ProductSnapshot(sku=f"SKU{i}", name="A", price=10.0, stock=float(i + 1))
        for i in range(501)
    ]
    erp = _FakeERP(current)
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    result = await use_case.run(tenant, {}, dry_run=False, force=True)

    assert not isinstance(result, NeedsConfirmation)
    assert len(result) == 501
    assert storefront.write_calls == 3  # 501 variants / 250 batch size, ceil


async def test_non_force_run_is_still_guarded(tenant):
    """Sanity check: force defaults to False, so existing guard behavior for
    a normal (non-force) call that would trip the guard is unchanged."""
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [
        ProductSnapshot(sku=f"SKU{i}", name="A", price=10.0, stock=float(i + 1))
        for i in range(501)
    ]
    erp = _FakeERP(current)
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    result = await use_case.run(tenant, {}, dry_run=False, force=False)

    assert isinstance(result, NeedsConfirmation)
    assert storefront.write_calls == 0


async def test_since_is_forwarded_to_erp(tenant):
    from datetime import UTC, datetime

    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    erp = _FakeERP([])
    storefront = _FakeStorefront()
    use_case = SyncInventoryToShopify(erp, storefront)

    when = datetime(2026, 1, 1, tzinfo=UTC)
    await use_case.run(tenant, {}, since=when)

    assert erp.received_since == when


async def test_run_resolves_variants_from_bulk_map_including_drafts(tenant):
    """Regression test for the production bug: find_variant_by_sku's
    search-index lookup misses DRAFT products, so a freshly rebuilt
    (all-draft) catalog matched 0 variants and stock was never pushed. The
    bulk map (built from a full listing, which does see drafts) must be
    used instead, and find_variant_by_sku must not be called at all."""
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [ProductSnapshot(sku="DRAFT-SKU", name="A", price=10.0, stock=5.0)]
    erp = _FakeERP(current)
    storefront = _FakeStorefrontBulkMap({"DRAFT-SKU": "gid://InventoryItem/1"})
    use_case = SyncInventoryToShopify(erp, storefront)

    result = await use_case.run(tenant, {}, dry_run=False)

    assert not storefront.search_called
    assert storefront.write_calls == 1
    assert storefront.pushed_deltas[0].variant_inventory_item_id == "gid://InventoryItem/1"
    assert len(result) == 1


async def test_run_skips_skus_missing_from_bulk_map(tenant):
    """A SKU absent from the bulk map (no matching Shopify variant at all)
    must not be pushed, and must not appear among the pushed deltas."""
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [
        ProductSnapshot(sku="KNOWN", name="A", price=10.0, stock=5.0),
        ProductSnapshot(sku="MISSING", name="B", price=20.0, stock=2.0),
    ]
    erp = _FakeERP(current)
    storefront = _FakeStorefrontBulkMap({"KNOWN": "gid://InventoryItem/1"})
    use_case = SyncInventoryToShopify(erp, storefront)

    await use_case.run(tenant, {}, dry_run=False)

    pushed_skus = {d.sku for d in storefront.pushed_deltas}
    assert pushed_skus == {"KNOWN"}


async def test_dry_run_report_counts_skipped_missing_variant_via_bulk_map(tenant):
    """Dry-run's skipped_missing_variant must reflect the bulk map, not a
    per-SKU search, so operators see an accurate count for draft-heavy
    catalogs before committing to a real push."""
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [
        ProductSnapshot(sku="KNOWN", name="A", price=10.0, stock=5.0),
        ProductSnapshot(sku="MISSING", name="B", price=20.0, stock=2.0),
    ]
    erp = _FakeERP(current)
    storefront = _FakeStorefrontBulkMap({"KNOWN": "gid://InventoryItem/1"})
    use_case = SyncInventoryToShopify(erp, storefront)

    report = await use_case.run(tenant, {}, dry_run=True)

    assert report.would_update == 2
    assert report.skipped_missing_variant == 1
    assert storefront.write_calls == 0


async def test_force_still_pushes_via_bulk_map(tenant):
    """force=True must still resolve variants from the bulk map (not
    find_variant_by_sku) and push all matched deltas."""
    from bims_shopify.application.sync_inventory import SyncInventoryToShopify

    current = [
        ProductSnapshot(sku=f"SKU{i}", name="A", price=10.0, stock=float(i + 1))
        for i in range(5)
    ]
    sku_map = {f"SKU{i}": f"gid://InventoryItem/{i}" for i in range(5)}
    erp = _FakeERP(current)
    storefront = _FakeStorefrontBulkMap(sku_map)
    use_case = SyncInventoryToShopify(erp, storefront)

    result = await use_case.run(tenant, {}, dry_run=False, force=True)

    assert not storefront.search_called
    assert len(result) == 5
    assert storefront.write_calls == 1
    assert len(storefront.pushed_deltas) == 5
