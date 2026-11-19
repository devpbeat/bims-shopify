"""The ``warehouse_scope`` stock figure rule: the storefront is fed from a SELECTED
subset of BIMS warehouses.

OWNER DECISION (deliberate): BIMS is trusted literally inside the scope. When the
scoped warehouses report zero, the published quantity IS zero -- never the
unfiltered total. The accepted risk is a recurrence of the 2026-11-02 BIMS
warehouse-filter bug; it is made visible by ``DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL``
but never overridden.
"""
from __future__ import annotations

import pytest

from bims_shopify.domain.stock_consolidation import (
    DISCREPANCY_DETAIL_BELOW_TOTAL,
    DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL,
    STOCK_SCOPE_WAREHOUSE_IDS_KEY,
    StockConsolidation,
    StockDiscrepancy,
    StockFigureRule,
    decide_sellable_stock,
    resolve_stock_figure_rule,
    resolve_stock_scope_warehouse_ids,
    summarize_stock_discrepancies,
)

# ARTIGAS=24, ESPANA=26, MAYORISTA=27
ALL = {24: 3.0, 26: 5.0, 27: 10.0}


def _scope(by_warehouse, total, scope=(27,), **kwargs):
    return decide_sellable_stock(
        by_warehouse=by_warehouse,
        unfiltered_total=total,
        rule=StockFigureRule.WAREHOUSE_SCOPE,
        scope_warehouse_ids=list(scope),
        **kwargs,
    )


def test_rule_value_is_stable():
    assert StockFigureRule.WAREHOUSE_SCOPE.value == "warehouse_scope"
    assert resolve_stock_figure_rule({"stock_figure_rule": "warehouse_scope"}) is (
        StockFigureRule.WAREHOUSE_SCOPE
    )


# --- resolver -------------------------------------------------------------


def test_resolver_returns_empty_when_absent_or_empty():
    assert resolve_stock_scope_warehouse_ids({}) == []
    assert resolve_stock_scope_warehouse_ids({STOCK_SCOPE_WAREHOUSE_IDS_KEY: None}) == []
    assert resolve_stock_scope_warehouse_ids({STOCK_SCOPE_WAREHOUSE_IDS_KEY: []}) == []


def test_resolver_accepts_ints_and_int_like_strings():
    assert resolve_stock_scope_warehouse_ids({STOCK_SCOPE_WAREHOUSE_IDS_KEY: [27]}) == [27]
    assert resolve_stock_scope_warehouse_ids({STOCK_SCOPE_WAREHOUSE_IDS_KEY: ["27", 24]}) == [27, 24]


def test_resolver_deduplicates_keeping_order():
    assert resolve_stock_scope_warehouse_ids({STOCK_SCOPE_WAREHOUSE_IDS_KEY: [27, "27", 24]}) == [27, 24]


@pytest.mark.parametrize(
    "bad",
    ["27", 27, ["abc"], [None], [True], [27.5], [-1], [0], ["0"], [""], {"a": 1}, [[27]]],
)
def test_resolver_raises_on_anything_non_coercible(bad):
    with pytest.raises(ValueError, match=STOCK_SCOPE_WAREHOUSE_IDS_KEY):
        resolve_stock_scope_warehouse_ids({STOCK_SCOPE_WAREHOUSE_IDS_KEY: bad})


# --- decision -------------------------------------------------------------


def test_scope_isolation_only_the_selected_warehouse_counts():
    decision = _scope(ALL, total=18.0)

    assert decision.quantity == 10.0  # not 18, not 3 or 5


def test_scope_with_several_warehouses_sums_only_those():
    assert _scope(ALL, total=18.0, scope=(26, 27)).quantity == 15.0


def test_positive_only_is_still_applied_inside_the_scope():
    decision = _scope({24: 4.0, 26: -2.0, 27: 10.0}, total=12.0, scope=(26, 27))

    assert decision.quantity == 10.0  # the negative scoped warehouse is ignored


def test_net_total_consolidation_is_honoured_inside_the_scope():
    decision = _scope(
        {26: -2.0, 27: 10.0}, total=8.0, scope=(26, 27), consolidation=StockConsolidation.NET_TOTAL
    )

    assert decision.quantity == 8.0


def test_scoped_zero_publishes_zero_even_when_the_total_is_positive():
    # DELIBERATE OWNER DECISION ("siempre se toca la cantidad de acuerdo a como
    # BIMS trae"): BIMS is trusted literally inside the scope. Falling back to the
    # unfiltered total here would defeat the point of selecting warehouses.
    decision = _scope({24: 7.0, 26: 0.0, 27: 0.0}, total=7.0)

    assert decision.quantity == 0.0


def test_scoped_negative_only_also_publishes_zero_not_none():
    assert _scope({27: -3.0}, total=-3.0).quantity == 0.0


def test_a_real_zero_answered_for_a_scoped_warehouse_is_zero_not_unknown():
    assert _scope({27: 0.0}, total=None).quantity == 0.0


def test_sku_never_mentioned_by_bims_is_unknown_not_zero():
    decision = _scope({}, total=None)

    assert decision.quantity is None
    assert decision.discrepancy is None


def test_sku_mentioned_only_for_unscoped_warehouses_and_no_total_is_unknown():
    assert _scope({24: 5.0}, total=None).quantity is None


def test_no_scoped_entry_but_bims_knows_the_sku_is_a_literal_zero_with_evidence():
    # BIMS answered the unfiltered total but said nothing for the scoped warehouse.
    decision = _scope({24: 5.0}, total=5.0)

    assert decision.quantity == 0.0
    assert decision.discrepancy is not None
    assert decision.discrepancy.kind == DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL


@pytest.mark.parametrize("scope", [None, []])
def test_scope_rule_without_scope_ids_raises(scope):
    with pytest.raises(ValueError, match="scope"):
        decide_sellable_stock(
            by_warehouse=ALL,
            unfiltered_total=18.0,
            rule=StockFigureRule.WAREHOUSE_SCOPE,
            scope_warehouse_ids=scope,
        )


def test_the_aggregate_key_is_never_a_scoped_warehouse():
    # warehouse_detail=False fakes {0: total}: no real warehouse identity.
    with pytest.raises(ValueError, match="positive"):
        _scope({0: 9.0}, total=9.0, scope=(0,))
    # And with a real scope, key 0 is simply not selected.
    assert _scope({0: 9.0}, total=None, scope=(27,)).quantity is None


def test_other_rules_ignore_scope_ids_entirely():
    decision = decide_sellable_stock(
        by_warehouse=ALL,
        unfiltered_total=18.0,
        rule=StockFigureRule.DETAIL_OR_TOTAL,
        scope_warehouse_ids=[27],
    )

    assert decision.quantity == 18.0


# --- discrepancy ----------------------------------------------------------


def test_scope_zero_below_total_fires_when_scoped_is_zero_and_total_positive():
    decision = _scope({24: 7.0, 27: 0.0}, total=7.0)

    d = decision.discrepancy
    assert d is not None
    assert d.kind == DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL
    assert d.pushed == 0.0  # evidence only: the published figure is unchanged
    assert d.unfiltered_total == 7.0
    assert d.detail_net == 0.0
    assert d.by_warehouse == {24: 7.0, 27: 0.0}


def test_scope_zero_signal_does_not_fire_when_scoped_is_positive():
    assert _scope(ALL, total=18.0).discrepancy is None  # a subset differing is expected


def test_scope_zero_signal_does_not_fire_when_total_is_zero_or_negative():
    assert _scope({27: 0.0}, total=0.0).discrepancy is None
    assert _scope({27: -2.0}, total=-2.0).discrepancy is None


def test_default_rule_discrepancy_is_unchanged_by_the_new_kind():
    decision = decide_sellable_stock(by_warehouse={27: 0.0}, unfiltered_total=6.0)

    assert decision.discrepancy is not None
    assert decision.discrepancy.kind == DISCREPANCY_DETAIL_BELOW_TOTAL


def test_summary_counts_the_new_kind():
    found = [_scope({27: 0.0}, total=4.0, sku=f"S{i}").discrepancy for i in range(3)]
    summary = summarize_stock_discrepancies([d for d in found if d is not None])

    assert summary["count"] == 3
    assert summary["by_kind"] == {DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL: 3}
    assert isinstance(found[0], StockDiscrepancy)
