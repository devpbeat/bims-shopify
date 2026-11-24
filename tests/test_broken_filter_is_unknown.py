"""An all-zero warehouse breakdown is BIMS's broken filter, not a quantity.

BIMS's per-warehouse filter answers 0 for every warehouse on SKUs that hold
stock (measured 2026-11-23 on SKU 112743: all five warehouses 0, unfiltered
total 20, Shopify holding 20). When the unfiltered total is ALSO missing,
there is no evidence of any quantity -- yet the breakdown is a non-empty dict
of zeros, so it used to read as a known 0 and get pushed, zeroing live stock.

The project's rule is older than this bug: absent stock is UNKNOWN and is
never pushed as zero.
"""
from __future__ import annotations

from bims_shopify.domain.stock_consolidation import (
    StockFigureRule,
    decide_sellable_stock,
)

ALL_ZERO = {20: 0.0, 24: 0.0, 25: 0.0, 26: 0.0, 27: 0.0}


def test_all_zero_warehouses_with_no_total_is_unknown_not_zero():
    decision = decide_sellable_stock(by_warehouse=ALL_ZERO, unfiltered_total=None, sku="112743")
    assert decision.quantity is None


def test_the_unfiltered_total_still_wins_when_bims_answers_it():
    decision = decide_sellable_stock(by_warehouse=ALL_ZERO, unfiltered_total=20.0, sku="112743")
    assert decision.quantity == 20.0
    assert decision.discrepancy is not None


def test_a_real_zero_is_still_a_real_zero():
    # BIMS answered the unfiltered query and said zero: that is evidence.
    decision = decide_sellable_stock(by_warehouse=ALL_ZERO, unfiltered_total=0.0, sku="X")
    assert decision.quantity == 0.0


def test_one_positive_warehouse_is_evidence_even_without_a_total():
    decision = decide_sellable_stock(
        by_warehouse={24: 0.0, 27: 10.0}, unfiltered_total=None, sku="X"
    )
    assert decision.quantity == 10.0


def test_negative_only_warehouses_without_a_total_are_still_a_known_zero():
    # A negative is a real ledger entry, not the broken filter's silence.
    decision = decide_sellable_stock(by_warehouse={24: -3.0}, unfiltered_total=None, sku="X")
    assert decision.quantity == 0.0


def test_detail_only_mode_also_refuses_to_invent_a_zero():
    decision = decide_sellable_stock(
        by_warehouse=ALL_ZERO, unfiltered_total=None, rule=StockFigureRule.DETAIL_ONLY, sku="X"
    )
    assert decision.quantity is None
