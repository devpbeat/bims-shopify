"""The mass-zero guard must weigh UNITS removed, not just how many SKUs hit 0.

In a clothing catalogue most sizes carry one or two units, so an ordinary day
of sales zeroes a large share of whatever changed. Measured in production
2026-11-24: of 149 changed SKUs, 87 went to zero (58%) -- and every delta was
-1, -2 or -3, i.e. things sold. Counting SKUs alone cannot tell that apart
from BIMS zeroing 2,448 SKUs that held hundreds of units each, so the guard
blocked a normal day and the store went four days without updating.

Units are what distinguishes the two. The guard now needs BOTH a high zero
ratio AND a material share of the shop's stock disappearing.
"""
from __future__ import annotations

from bims_shopify.application.sync_inventory import check_safety_guard
from bims_shopify.domain.product import InventoryDelta


def _sales_day() -> list[InventoryDelta]:
    """87 single-unit SKUs sold out, 62 reduced by a few. The real 2026-11-24 shape."""
    out = [
        InventoryDelta(sku=f"Z{i}", previous_stock=1.0, new_stock=0.0) for i in range(87)
    ]
    out += [
        InventoryDelta(sku=f"P{i}", previous_stock=11.0, new_stock=9.0) for i in range(62)
    ]
    return out


def _catalog_wipe() -> list[InventoryDelta]:
    """BIMS answers 0 for SKUs holding real stock: the 2026-11-19 incident."""
    return [
        InventoryDelta(sku=f"W{i}", previous_stock=120.0, new_stock=0.0) for i in range(2448)
    ]


def test_an_ordinary_day_of_sales_is_not_blocked(tenant):
    assert check_safety_guard(tenant, _sales_day()) is None


def test_a_catalog_wipe_is_still_blocked(tenant):
    verdict = check_safety_guard(tenant, _catalog_wipe())
    assert verdict is not None
    assert verdict.reason == "zero_ratio_exceeded"


def test_many_small_zeroes_do_not_add_up_to_a_wipe(tenant):
    # 300 SKUs of one unit each: still only 300 units, still ordinary trade.
    deltas = [
        InventoryDelta(sku=f"S{i}", previous_stock=1.0, new_stock=0.0) for i in range(300)
    ]
    assert check_safety_guard(tenant, deltas) is None


def test_a_handful_of_big_SKUs_emptying_is_blocked(tenant):
    # Few SKUs, but they hold the shop's inventory: that is the dangerous shape.
    deltas = [
        InventoryDelta(sku=f"B{i}", previous_stock=500.0, new_stock=0.0) for i in range(40)
    ]
    verdict = check_safety_guard(tenant, deltas)
    assert verdict is not None


def test_the_absolute_changed_cap_still_applies(tenant):
    tenant.field_mappings = {**tenant.field_mappings, "max_changed_variants": 3}
    deltas = [
        InventoryDelta(sku=f"C{i}", previous_stock=5.0, new_stock=6.0) for i in range(10)
    ]
    verdict = check_safety_guard(tenant, deltas)
    assert verdict is not None
    assert verdict.reason == "max_changed_exceeded"
