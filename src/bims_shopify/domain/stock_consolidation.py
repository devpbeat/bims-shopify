"""How per-warehouse BIMS quantities become the single sellable figure
published to Shopify.

There is one rule and every consolidation path must use it, so the storefront
can never disagree with the stock gate that decided to publish.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


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


class StockFigureRule(str, Enum):
    """Which BIMS evidence decides the quantity pushed to Shopify.

    BIMS gives two independent figures per SKU and each fails differently:
    the per-warehouse answers (``warehouse_ids`` in the payload) can come back
    ALL ZERO for stock that exists -- BIMS's warehouse filter is broken -- and
    the unfiltered total lets a negative warehouse cancel a positive one.
    """

    #: Default. The larger of the consolidated per-warehouse figure and the
    #: unfiltered total (clamped at 0). Each figure is a lower bound of what
    #: can be shipped that is immune to the OTHER figure's failure, so neither
    #: failure mode can zero a sellable SKU. The price: stock in a warehouse
    #: the tenant does not sync can be counted, which is reported as a
    #: discrepancy on every pull.
    DETAIL_OR_TOTAL = "detail_or_total"
    #: The consolidated per-warehouse figure only (the pre-2026-11-02
    #: behaviour; zero for the whole catalog while BIMS's filter is broken).
    DETAIL_ONLY = "detail_only"
    #: The unfiltered total only, clamped at 0 (negatives cancel positives).
    TOTAL_ONLY = "total_only"


DEFAULT_STOCK_FIGURE_RULE = StockFigureRule.DETAIL_OR_TOTAL

#: ``field_mappings`` key a tenant uses to pick its ``StockFigureRule``.
STOCK_FIGURE_RULE_KEY = "stock_figure_rule"

DISCREPANCY_DETAIL_BELOW_TOTAL = "detail_below_total"
DISCREPANCY_DETAIL_ABOVE_TOTAL = "detail_above_total"

_EPSILON = 1e-9


def resolve_stock_figure_rule(field_mappings: dict[str, Any]) -> StockFigureRule:
    """The tenant's rule; the default when unset, an error when unknown.

    An unrecognised value is refused rather than defaulted: a typo silently
    selecting another rule is how a catalog ends up at the wrong quantity.
    """
    raw = field_mappings.get(STOCK_FIGURE_RULE_KEY)
    if raw is None or raw == "":
        return DEFAULT_STOCK_FIGURE_RULE
    try:
        return StockFigureRule(raw)
    except ValueError:
        allowed = ", ".join(rule.value for rule in StockFigureRule)
        raise ValueError(
            f"invalid {STOCK_FIGURE_RULE_KEY}={raw!r}; expected one of: {allowed}"
        ) from None


@dataclass(frozen=True)
class StockDiscrepancy:
    """The per-warehouse breakdown and the unfiltered total disagree for a SKU."""

    sku: str
    kind: str
    detail_net: float
    unfiltered_total: float
    pushed: float | None
    by_warehouse: dict[int, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "kind": self.kind,
            "detail_net": self.detail_net,
            "unfiltered_total": self.unfiltered_total,
            "pushed": self.pushed,
            "by_warehouse": {str(wid): qty for wid, qty in self.by_warehouse.items()},
        }


@dataclass(frozen=True)
class StockDecision:
    quantity: float | None
    discrepancy: StockDiscrepancy | None


def decide_sellable_stock(
    *,
    by_warehouse: dict[int, float],
    unfiltered_total: float | None,
    rule: StockFigureRule = DEFAULT_STOCK_FIGURE_RULE,
    consolidation: StockConsolidation = DEFAULT_STOCK_CONSOLIDATION,
    sku: str = "",
) -> StockDecision:
    """The quantity to push for one SKU, and the discrepancy behind it if any.

    ``by_warehouse`` is the unclamped per-warehouse answer (may be empty) and
    ``unfiltered_total`` BIMS's own total without any warehouse filter (``None``
    when BIMS did not answer for the SKU). ``quantity`` is ``None`` only when
    neither figure exists: unknown stock is never pushed as zero.

    A discrepancy is raised whenever the NET of the warehouses differs from the
    unfiltered total. BIMS's own total is that net, so a negative warehouse
    alone is NOT a discrepancy (10 + -3 = 7 agrees with 7); a filter returning
    zeros for stock that exists is.
    """
    detail = consolidate_sellable_stock(by_warehouse, mode=consolidation) if by_warehouse else None
    total = None if unfiltered_total is None else max(0.0, float(unfiltered_total))

    if rule is StockFigureRule.DETAIL_ONLY:
        quantity = detail
    elif rule is StockFigureRule.TOTAL_ONLY:
        quantity = total
    else:
        known = [figure for figure in (detail, total) if figure is not None]
        quantity = max(known) if known else None

    discrepancy: StockDiscrepancy | None = None
    if unfiltered_total is not None:
        net = float(sum(by_warehouse.values()))
        if abs(net - float(unfiltered_total)) > _EPSILON:
            discrepancy = StockDiscrepancy(
                sku=sku,
                kind=(
                    DISCREPANCY_DETAIL_BELOW_TOTAL
                    if net < float(unfiltered_total)
                    else DISCREPANCY_DETAIL_ABOVE_TOTAL
                ),
                detail_net=net,
                unfiltered_total=float(unfiltered_total),
                pushed=quantity,
                by_warehouse=dict(by_warehouse),
            )
    return StockDecision(quantity=quantity, discrepancy=discrepancy)


#: SKUs quoted in the log event / audit entry (the counter always has them all).
DISCREPANCY_SAMPLE_SIZE = 20


def summarize_stock_discrepancies(
    discrepancies: list[StockDiscrepancy], sample_size: int = DISCREPANCY_SAMPLE_SIZE
) -> dict[str, Any]:
    """Counter, per-kind breakdown and a bounded sample, for logs, audit and the portal."""
    by_kind: dict[str, int] = {}
    for discrepancy in discrepancies:
        by_kind[discrepancy.kind] = by_kind.get(discrepancy.kind, 0) + 1
    return {
        "count": len(discrepancies),
        "by_kind": by_kind,
        "sample": [d.as_dict() for d in discrepancies[:sample_size]],
    }
