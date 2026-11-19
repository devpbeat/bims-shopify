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
    #: Only the warehouses the tenant selected (``stock_scope_warehouse_ids``),
    #: consolidated with the tenant's ``StockConsolidation``. BIMS is trusted
    #: LITERALLY inside the scope: scoped warehouses at zero publish zero, with
    #: no fallback to the unfiltered total. See ``decide_sellable_stock``.
    WAREHOUSE_SCOPE = "warehouse_scope"


DEFAULT_STOCK_FIGURE_RULE = StockFigureRule.DETAIL_OR_TOTAL

#: ``field_mappings`` key a tenant uses to pick its ``StockFigureRule``.
STOCK_FIGURE_RULE_KEY = "stock_figure_rule"

DISCREPANCY_DETAIL_BELOW_TOTAL = "detail_below_total"
DISCREPANCY_DETAIL_ABOVE_TOTAL = "detail_above_total"
#: ``WAREHOUSE_SCOPE`` only: the scoped quantity is 0 while BIMS's unfiltered
#: total is positive. This is the fingerprint of the 2026-11-02 BIMS filter bug
#: (or of a legitimately empty scope); it is evidence, never a correction.
DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL = "scope_zero_below_total"

#: ``field_mappings`` key holding the warehouse ids that feed the storefront
#: under ``StockFigureRule.WAREHOUSE_SCOPE`` (a JSON list of ints).
STOCK_SCOPE_WAREHOUSE_IDS_KEY = "stock_scope_warehouse_ids"

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


def _coerce_warehouse_id(value: Any) -> int | None:
    """An int, or a string of digits (ids arrive through JSON); anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def resolve_stock_scope_warehouse_ids(field_mappings: dict[str, Any]) -> list[int]:
    """The tenant's selected warehouse ids; ``[]`` when unset, an error when malformed.

    Accepts a list of ints or int-like strings (``["27"]`` -> ``[27]``, since it
    arrives via JSON); duplicates collapse keeping first-seen order. Ids must be
    positive: ``0`` is reserved by the adapter as the stand-in key of the
    aggregate (no warehouse identity) breakdown and can never be a real
    warehouse. Anything else raises ``ValueError``: like
    ``resolve_stock_figure_rule``, a typo must never silently select another scope.
    """
    raw = field_mappings.get(STOCK_SCOPE_WAREHOUSE_IDS_KEY)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"invalid {STOCK_SCOPE_WAREHOUSE_IDS_KEY}={raw!r}; expected a list of warehouse ids"
        )
    ids: list[int] = []
    for item in raw:
        warehouse_id = _coerce_warehouse_id(item)
        if warehouse_id is None or warehouse_id <= 0:
            raise ValueError(
                f"invalid {STOCK_SCOPE_WAREHOUSE_IDS_KEY} entry {item!r}; "
                "expected a positive integer warehouse id"
            )
        if warehouse_id not in ids:
            ids.append(warehouse_id)
    return ids


def validate_stock_field_mappings(field_mappings: dict[str, Any]) -> None:
    """Raise ``ValueError`` if the stock settings in ``field_mappings`` cannot be used.

    Run at write time (API) so a bad rule or scope is refused up front instead of
    failing inside a background pull. Built only on the two resolvers, plus the one
    cross-field rule: ``WAREHOUSE_SCOPE`` needs a non-empty scope.
    """
    rule = resolve_stock_figure_rule(field_mappings)
    scope = resolve_stock_scope_warehouse_ids(field_mappings)
    if rule is StockFigureRule.WAREHOUSE_SCOPE and not scope:
        raise ValueError(
            f"{STOCK_FIGURE_RULE_KEY}={rule.value} requires a non-empty "
            f"{STOCK_SCOPE_WAREHOUSE_IDS_KEY}"
        )


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
    scope_warehouse_ids: list[int] | None = None,
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

    ``WAREHOUSE_SCOPE`` (``scope_warehouse_ids`` required, keyword-only):

    * The quantity comes ONLY from the ``by_warehouse`` entries whose key is in
      ``scope_warehouse_ids``, consolidated with ``consolidation`` (so
      ``POSITIVE_ONLY`` still ignores a negative scoped warehouse).
    * OWNER DECISION -- BIMS is trusted literally. If the scoped warehouses are
      all zero or negative the published quantity IS ``0.0``: no fallback to the
      unfiltered total, and never ``None``. In the owner's words, "siempre se
      toca la cantidad de acuerdo a como BIMS trae, confiamos en que
      configuraron bien su deposito". ACCEPTED RISK: BIMS's warehouse filter
      once answered 0 for stock that exists (2026-11-02, the outage that zeroed
      the whole catalog); under this rule such an answer WILL be published as 0.
      The trade is deliberate and is made visible, not prevented, by
      ``DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL``.
    * ``None`` (UNKNOWN, never pushed as zero) only when there is NO stock
      evidence at all: ``by_warehouse`` has no entry for any scoped id AND
      ``unfiltered_total`` is ``None`` -- BIMS never mentioned the SKU. If BIMS
      answered for a scoped warehouse (even 0.0), that is a real 0. If BIMS
      knows the SKU (``unfiltered_total`` present) but said nothing for the
      scoped warehouses, the scope holds none of it: 0.0, with the discrepancy
      below when the total is positive.
    * Discrepancy: the usual net-vs-total comparison is NOT made (a subset
      differs from the total almost always, which would be noise on every SKU).
      Only ``DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL`` is raised, when the scoped
      quantity is 0.0 while ``unfiltered_total`` > 0. It never changes the
      quantity.
    * Misconfiguration is loud: ``scope_warehouse_ids`` empty/``None`` or holding
      a non-positive id raises ``ValueError``. Key ``0`` is the adapter's
      stand-in for "all warehouses together" and carries no real identity, so it
      can never be selected.
    """
    if rule is StockFigureRule.WAREHOUSE_SCOPE:
        return _decide_warehouse_scope(
            by_warehouse=by_warehouse,
            unfiltered_total=unfiltered_total,
            consolidation=consolidation,
            sku=sku,
            scope_warehouse_ids=scope_warehouse_ids,
        )
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


def _decide_warehouse_scope(
    *,
    by_warehouse: dict[int, float],
    unfiltered_total: float | None,
    consolidation: StockConsolidation,
    sku: str,
    scope_warehouse_ids: list[int] | None,
) -> StockDecision:
    if not scope_warehouse_ids:
        raise ValueError(
            f"rule {StockFigureRule.WAREHOUSE_SCOPE.value} needs a non-empty "
            f"{STOCK_SCOPE_WAREHOUSE_IDS_KEY} (the selected warehouse scope)"
        )
    if any(isinstance(wid, bool) or not isinstance(wid, int) or wid <= 0 for wid in scope_warehouse_ids):
        raise ValueError(
            f"{STOCK_SCOPE_WAREHOUSE_IDS_KEY} must hold positive integer warehouse ids, "
            f"got {list(scope_warehouse_ids)!r}"
        )
    scope = set(scope_warehouse_ids)
    scoped = {wid: qty for wid, qty in by_warehouse.items() if wid in scope}

    if scoped:
        quantity: float | None = consolidate_sellable_stock(scoped, mode=consolidation)
    elif unfiltered_total is not None:
        quantity = 0.0
    else:
        quantity = None

    discrepancy: StockDiscrepancy | None = None
    if quantity == 0.0 and unfiltered_total is not None and float(unfiltered_total) > _EPSILON:
        discrepancy = StockDiscrepancy(
            sku=sku,
            kind=DISCREPANCY_SCOPE_ZERO_BELOW_TOTAL,
            detail_net=float(sum(scoped.values())),
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
