"""Per-SKU detail report produced by SyncInventoryToShopify (observability only)."""
import pytest

from bims_shopify.application import sync_inventory
from bims_shopify.application.sync_inventory import NeedsConfirmation, SyncInventoryToShopify
from bims_shopify.domain.product import ProductSnapshot


class _ERP:
    def __init__(self, products):
        self._products = products

    async def list_products(self, tenant, since=None):
        return self._products


class _Storefront:
    def __init__(self, sku_map, fail_batches=()):
        self.sku_map = sku_map
        self.fail_batches = set(fail_batches)
        self.pushed = []
        self.calls = 0

    async def build_sku_inventory_map(self, tenant):
        return self.sku_map

    async def set_inventory_quantities(self, tenant, deltas):
        self.calls += 1
        if self.calls in self.fail_batches:
            raise RuntimeError("shopify exploded")
        self.pushed.extend(deltas)


def _p(sku, stock):
    return ProductSnapshot(sku=sku, name="n", price=1.0, stock=stock)


def _map(*skus):
    return {s: f"gid://{s}" for s in skus}


async def _run(tenant, products, sku_map, previous=None, **kw):
    sf = _Storefront(sku_map, kw.pop("fail_batches", ()))
    uc = SyncInventoryToShopify(_ERP(products), sf)
    result = await uc.run(tenant, previous or {}, **kw)
    return uc, sf, result


async def test_each_bucket_is_populated(tenant):
    products = [
        _p("UPD", 5.0),  # changed
        _p("SAME", 3.0),  # unchanged
        _p("ZERO", 0.0),  # goes to zero
        _p("ZSAME", 0.0),  # already zero
        _p("NOSHOP", 4.0),  # no variant
        _p("UNKNOWN", None),  # in shopify, BIMS has no stock record
    ]
    sku_map = _map("UPD", "SAME", "ZERO", "ZSAME", "UNKNOWN", "ONLYSHOP")
    previous = {"UPD": 2.0, "SAME": 3.0, "ZERO": 9.0, "ZSAME": 0.0}
    tenant.field_mappings = {**tenant.field_mappings, "max_zero_ratio": 1.0}
    uc, sf, result = await _run(tenant, products, sku_map, previous)
    r = uc.last_report
    assert r["scanned"] == {"bims": 6, "shopify": 6}
    assert r["updated"]["count"] == 2
    assert {s["sku"] for s in r["updated"]["sample"]} == {"UPD", "ZERO"}
    upd = next(s for s in r["updated"]["sample"] if s["sku"] == "UPD")
    assert (upd["previous"], upd["new"]) == (2.0, 5.0)
    assert r["unchanged"]["count"] == 1
    assert r["unchanged"]["sample"][0]["sku"] == "SAME"
    assert r["zero_stock"]["count"] == 2  # ZERO (changed) + ZSAME (already zero)
    assert {s["sku"] for s in r["zero_stock"]["sample"]} == {"ZERO", "ZSAME"}
    assert r["not_in_shopify"]["count"] == 1
    assert r["not_in_shopify"]["sample"][0]["sku"] == "NOSHOP"
    assert r["not_in_bims"]["count"] == 2
    assert {s["sku"] for s in r["not_in_bims"]["sample"]} == {"UNKNOWN", "ONLYSHOP"}
    assert r["failed"]["count"] == 0
    # return value unchanged: the deltas list
    assert {d.sku for d in result} == {"UPD", "ZERO", "NOSHOP"}


async def test_absent_stock_is_not_in_bims_and_never_pushed(tenant):
    uc, sf, result = await _run(tenant, [_p("A", None)], _map("A"))
    assert result == []
    assert sf.pushed == []
    assert uc.last_report["not_in_bims"]["count"] == 1
    assert uc.last_report["zero_stock"]["count"] == 0


async def test_incremental_run_does_not_claim_not_in_bims(tenant):
    from datetime import UTC, datetime

    uc, _, _ = await _run(
        tenant, [_p("A", 1.0)], _map("A", "B"), since=datetime(2026, 1, 1, tzinfo=UTC)
    )
    nib = uc.last_report["not_in_bims"]
    assert nib["count"] is None
    assert nib["sample"] == []


async def test_samples_are_bounded_but_counts_are_full(tenant, monkeypatch):
    for name in (
        "UPDATED_SAMPLE_SIZE",
        "UNCHANGED_SAMPLE_SIZE",
        "NOT_IN_SHOPIFY_SAMPLE_SIZE",
        "NOT_IN_BIMS_SAMPLE_SIZE",
        "ZERO_STOCK_SAMPLE_SIZE",
        "FAILED_SAMPLE_SIZE",
    ):
        monkeypatch.setattr(sync_inventory, name, 2)
    products = (
        [_p(f"U{i}", 5.0) for i in range(6)]
        + [_p(f"S{i}", 3.0) for i in range(6)]
        + [_p(f"N{i}", 1.0) for i in range(6)]
    )
    previous = {f"S{i}": 3.0 for i in range(6)}
    sku_map = _map(*[f"U{i}" for i in range(6)], *[f"S{i}" for i in range(6)],
                   *[f"X{i}" for i in range(6)])
    uc, _, _ = await _run(tenant, products, sku_map, previous, force=True)
    r = uc.last_report
    for key in ("updated", "unchanged", "not_in_shopify", "not_in_bims"):
        assert len(r[key]["sample"]) <= 2
    assert r["updated"]["count"] == 6
    assert r["unchanged"]["count"] == 6
    assert r["not_in_shopify"]["count"] == 6
    assert r["not_in_bims"]["count"] == 6


async def test_batch_failure_is_recorded_and_still_raises(tenant):
    products = [_p(f"S{i}", 5.0) for i in range(3)]
    sf = _Storefront(_map("S0", "S1", "S2"), fail_batches={1})
    uc = SyncInventoryToShopify(_ERP(products), sf)
    with pytest.raises(RuntimeError, match="shopify exploded"):
        await uc.run(tenant, {})
    r = uc.last_report
    assert r["failed"]["count"] == 3
    assert "shopify exploded" in r["failed"]["sample"][0]["error"]
    assert r["updated"]["count"] == 0


async def test_guard_trip_reports_numbers_and_nothing_updated(tenant):
    products = [_p(f"S{i}", 0.0) for i in range(10)]
    previous = {f"S{i}": 5.0 for i in range(10)}
    uc, sf, result = await _run(tenant, products, _map(*previous), previous)
    assert isinstance(result, NeedsConfirmation)
    assert sf.pushed == []
    r = uc.last_report
    g = r["guard"]
    assert g["fired"] is True
    assert g["reason"] == "zero_ratio_exceeded"
    assert g["zero_count"] == 10
    assert g["changed_count"] == 10
    assert g["zero_ratio"] == 1.0
    assert g["max_zero_ratio"] == 0.2
    assert g["max_changed_variants"] == 500
    assert r["updated"]["count"] == 0
    assert r["blocked_by_guard"]["count"] == 10


async def test_guard_numbers_present_when_it_does_not_fire(tenant):
    uc, _, _ = await _run(tenant, [_p("A", 3.0)], _map("A"), {"A": 8.0})
    g = uc.last_report["guard"]
    assert g["fired"] is False
    assert g["changed_count"] == 1
    assert g["zero_count"] == 0
    assert g["forced"] is False
