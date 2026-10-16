"""How per-warehouse BIMS quantities become the single sellable figure
published to Shopify.

There is one rule and every consolidation path must use it, so the storefront
can never disagree with the stock gate that decided to publish.
"""
from __future__ import annotations

from enum import Enum


class StockConsolidation(str, Enum):
    """How to collapse per-warehouse quantities into one sellable number."""

    POSITIVE_ONLY = "positive_only"
    NET_TOTAL = "net_total"


DEFAULT_STOCK_CONSOLIDATION = StockConsolidation.POSITIVE_ONLY


def consolidate_sellable_stock(
    by_warehouse: dict[int, float],
    *,
    mode: StockConsolidation = DEFAULT_STOCK_CONSOLIDATION,
) -> float:
    """Collapse ``by_warehouse`` into the quantity to publish.

    ``POSITIVE_ONLY`` (the default) sums only the warehouses holding a
    positive quantity and ignores the zero and negative ones.

    A negative warehouse at this client is a bookkeeping artifact, not missing
    merchandise: stock is transferred from the Mayorista depot to a store, the
    receiving staff do not register the receipt in BIMS, the item is sold
    anyway, and that warehouse goes negative. Letting it cancel a positive
    warehouse would hide stock that is physically there and lose the sale --
    the store ships from whichever location holds the item.

    Worked example, measured on OTTO BOXER PREMIUM GREEN MOSS (2026-10-15):
    talle M is ARTIGAS -3 and MAYORISTA 10. ``NET_TOTAL`` yields 7, which is
    what BIMS's own pre-aggregated figure returns; ``POSITIVE_ONLY`` yields
    10, the quantity the store can actually ship.

    ``NET_TOTAL`` reproduces the previous behaviour (sum everything, then
    clamp a negative total to zero) and stays available per tenant so the
    change can be reverted without a deploy.

    Returns 0.0 when nothing qualifies. Callers must keep treating "no stock
    record at all" as UNKNOWN and never push it as zero -- that is a separate
    rule and this function never sees those SKUs.
    """
    if not by_warehouse:
        return 0.0
    if mode is StockConsolidation.NET_TOTAL:
        return max(0.0, float(sum(by_warehouse.values())))
    return float(sum(quantity for quantity in by_warehouse.values() if quantity > 0))
