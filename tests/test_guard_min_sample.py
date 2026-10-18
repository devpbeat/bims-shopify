"""The mass-zero ratio guard needs a minimum sample before it means anything.

It exists to catch "BIMS answered with garbage and the whole catalog went to
zero" -- an event measured in thousands of SKUs. With continuous pulls most
cycles carry only a handful of changes, where a single legitimately sold-out
item is 25% of the batch and would block the cycle until a human forced it.
Below the minimum sample the absolute cap still applies; only the ratio is
held back.
"""
from __future__ import annotations

from bims_shopify.application.sync_inventory import (
    GUARD_MIN_SAMPLE,
    check_safety_guard,
)
from bims_shopify.domain.product import InventoryDelta


def _deltas(total: int, zeros: int) -> list[InventoryDelta]:
    out = []
    for i in range(total):
        out.append(
            InventoryDelta(
                sku=f"S{i}",
                previous_stock=5.0,
                new_stock=0.0 if i < zeros else 7.0,
            )
        )
    return out


def test_one_sold_out_item_in_a_small_batch_does_not_block(tenant):
    # 1 of 4 is 25%, over the 20% ratio, but far below the minimum sample.
    assert check_safety_guard(tenant, _deltas(4, 1)) is None


def test_a_whole_small_batch_going_to_zero_still_does_not_block(tenant):
    # Four sold-out items is a normal Tuesday, not a catalog wipe.
    assert check_safety_guard(tenant, _deltas(4, 4)) is None


def test_the_ratio_still_fires_once_the_sample_is_meaningful(tenant):
    total = GUARD_MIN_SAMPLE
    verdict = check_safety_guard(tenant, _deltas(total, total))
    assert verdict is not None
    assert verdict.reason == "zero_ratio_exceeded"


def test_a_healthy_large_batch_still_passes(tenant):
    total = GUARD_MIN_SAMPLE * 4
    assert check_safety_guard(tenant, _deltas(total, total // 100)) is None


def test_the_absolute_cap_is_not_weakened_by_the_minimum_sample(tenant):
    tenant.field_mappings = {**tenant.field_mappings, "max_changed_variants": 3}
    verdict = check_safety_guard(tenant, _deltas(10, 0))
    assert verdict is not None
    assert verdict.reason == "max_changed_exceeded"
