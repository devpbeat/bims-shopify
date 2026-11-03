"""The explicit rule that decides the quantity pushed to Shopify.

BIMS reports stock two ways and each one fails differently:

* the per-warehouse answers (``warehouse_ids`` in the payload) can be ALL ZERO
  for stock that exists -- BIMS's warehouse filter is broken (2026-11-02,
  verified in production);
* the unfiltered total lets a negative warehouse cancel a positive one.

The rule must survive both, and every disagreement must be visible.
"""
from __future__ import annotations

import pytest

from bims_shopify.domain.stock_consolidation import (
    DISCREPANCY_DETAIL_ABOVE_TOTAL,
    DISCREPANCY_DETAIL_BELOW_TOTAL,
    StockConsolidation,
    StockFigureRule,
    decide_sellable_stock,
    resolve_stock_figure_rule,
)

# The live OTTO NEW BASIC SATORI MARFIL (S) answer on 2026-11-02.
ALL_ZERO = {20: 0.0, 24: 0.0, 25: 0.0, 26: 0.0, 27: 0.0}
# OTTO BOXER PREMIUM GREEN MOSS (M): a negative warehouse hides a positive one.
BOXER_M = {24: -3.0, 27: 10.0}


def test_regression_zero_per_warehouse_with_positive_unfiltered_total_is_sellable():
    """REGRESSION (Cyber Monday outage): never push 0 while BIMS says 6."""
    decision = decide_sellable_stock(by_warehouse=ALL_ZERO, unfiltered_total=6.0)

    assert decision.quantity == 6.0
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind == DISCREPANCY_DETAIL_BELOW_TOTAL


def test_negative_warehouse_does_not_cancel_a_positive_one():
    """The protection the old per-warehouse recompute existed for."""
    decision = decide_sellable_stock(by_warehouse=BOXER_M, unfiltered_total=7.0)

    assert decision.quantity == 10.0  # not the net 7 BIMS reports


def test_net_of_the_warehouses_equal_to_the_total_is_not_a_discrepancy():
    """BIMS's own total is the net of the warehouses: 10 + (-3) = 7 agrees."""
    assert decide_sellable_stock(by_warehouse=BOXER_M, unfiltered_total=7.0).discrepancy is None


def test_detail_above_total_is_reported_when_the_net_differs():
    decision = decide_sellable_stock(by_warehouse={1: 9.0}, unfiltered_total=4.0)

    assert decision.quantity == 9.0
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind == DISCREPANCY_DETAIL_ABOVE_TOTAL


def test_a_negative_unfiltered_total_never_pushes_a_negative_quantity():
    assert decide_sellable_stock(by_warehouse={1: 0.0}, unfiltered_total=-2.0).quantity == 0.0


def test_missing_unfiltered_total_uses_the_detail_and_reports_nothing():
    decision = decide_sellable_stock(by_warehouse=BOXER_M, unfiltered_total=None)

    assert decision.quantity == 10.0 and decision.discrepancy is None


def test_missing_detail_with_a_total_uses_the_total_and_reports_it():
    decision = decide_sellable_stock(by_warehouse={}, unfiltered_total=5.0)

    assert decision.quantity == 5.0
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind == DISCREPANCY_DETAIL_BELOW_TOTAL


def test_no_figure_at_all_is_unknown_not_zero():
    assert decide_sellable_stock(by_warehouse={}, unfiltered_total=None).quantity is None


def test_detail_only_rule_reproduces_the_old_behaviour_and_still_reports():
    decision = decide_sellable_stock(
        by_warehouse=ALL_ZERO, unfiltered_total=6.0, rule=StockFigureRule.DETAIL_ONLY
    )

    assert decision.quantity == 0.0
    assert decision.discrepancy is not None


def test_total_only_rule_pushes_the_clamped_total():
    decision = decide_sellable_stock(
        by_warehouse=BOXER_M, unfiltered_total=7.0, rule=StockFigureRule.TOTAL_ONLY
    )

    assert decision.quantity == 7.0


def test_consolidation_mode_still_applies_to_the_detail():
    decision = decide_sellable_stock(
        by_warehouse=BOXER_M,
        unfiltered_total=7.0,
        consolidation=StockConsolidation.NET_TOTAL,
    )

    assert decision.quantity == 7.0


def test_discrepancy_carries_the_evidence():
    discrepancy = decide_sellable_stock(by_warehouse=ALL_ZERO, unfiltered_total=6.0).discrepancy

    assert discrepancy is not None
    assert discrepancy.detail_net == 0.0 and discrepancy.unfiltered_total == 6.0
    assert discrepancy.pushed == 6.0


def test_default_rule_is_detail_or_total():
    assert resolve_stock_figure_rule({}) is StockFigureRule.DETAIL_OR_TOTAL


def test_rule_is_read_from_the_tenant_field_mappings():
    assert (
        resolve_stock_figure_rule({"stock_figure_rule": "detail_only"}) is StockFigureRule.DETAIL_ONLY
    )


def test_an_unknown_rule_is_refused_not_silently_defaulted():
    with pytest.raises(ValueError, match="stock_figure_rule"):
        resolve_stock_figure_rule({"stock_figure_rule": "whatever"})
