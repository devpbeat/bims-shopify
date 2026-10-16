"""The sellable quantity published to Shopify is the sum of the POSITIVE
warehouses only.

Negative warehouse quantities are a bookkeeping artifact at this client:
stock is transferred from the Mayorista depot to a store, staff do not
register the receipt in BIMS, the item is then sold, and the receiving
warehouse goes negative. The merchandise exists; the paperwork does not.
Letting that negative cancel a positive warehouse would hide sellable stock
and lose sales.
"""
from __future__ import annotations

import pytest

from bims_shopify.domain.stock_consolidation import (
    StockConsolidation,
    consolidate_sellable_stock,
)


# Measured on OTTO BOXER PREMIUM GREEN MOSS, 2026-10-15 (BIMS UI).
# ARTIGAS=24, ESPAÑA=26, MAYORISTA=27.
BOXER_M = {24: -3.0, 27: 10.0}
BOXER_L = {24: -5.0, 26: 1.0, 27: 15.0}
BOXER_S = {24: -2.0, 26: 2.0, 27: 37.0}


@pytest.mark.parametrize(
    ("by_warehouse", "expected"),
    [(BOXER_M, 10.0), (BOXER_L, 16.0), (BOXER_S, 39.0)],
)
def test_positive_only_ignores_negative_warehouses(by_warehouse, expected):
    assert (
        consolidate_sellable_stock(by_warehouse, mode=StockConsolidation.POSITIVE_ONLY)
        == expected
    )


@pytest.mark.parametrize(
    ("by_warehouse", "expected"),
    [(BOXER_M, 7.0), (BOXER_L, 11.0), (BOXER_S, 37.0)],
)
def test_net_total_reproduces_the_previous_behaviour(by_warehouse, expected):
    assert (
        consolidate_sellable_stock(by_warehouse, mode=StockConsolidation.NET_TOTAL)
        == expected
    )


def test_zero_warehouses_are_ignored_like_negatives():
    assert consolidate_sellable_stock({24: 0.0, 27: 4.0}) == 4.0


def test_all_zero_or_negative_consolidates_to_zero():
    assert consolidate_sellable_stock({24: -3.0, 26: 0.0}) == 0.0


def test_net_total_never_returns_a_negative():
    assert (
        consolidate_sellable_stock({24: -9.0, 27: 1.0}, mode=StockConsolidation.NET_TOTAL)
        == 0.0
    )


def test_no_warehouse_data_is_zero_not_an_error():
    assert consolidate_sellable_stock({}) == 0.0


def test_positive_only_is_the_default():
    assert consolidate_sellable_stock(BOXER_M) == 10.0


def test_raw_negatives_still_consolidate_correctly():
    """The per-warehouse detail now arrives unclamped, so the rule must do the
    ignoring -- not the fetch layer."""
    raw = {24: -3.0, 27: 10.0}
    assert consolidate_sellable_stock(raw) == 10.0
    assert consolidate_sellable_stock(raw, mode=StockConsolidation.NET_TOTAL) == 7.0
