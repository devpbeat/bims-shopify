"""Persisted BIMS catalog snapshot: the hand-off between the pull and push workers.

The PULL worker copies what BIMS reported (products, aggregated stock and the
per-warehouse stock behind it) into our own database. The PUSH worker never
talks to BIMS: it reads that snapshot, diffs it against what was last pushed
to Shopify and pushes the differences.

This module also owns the pure rules that turn a snapshot into the
``ProductSnapshot`` list the push use case consumes, so the live adapter and
the snapshot reader can never disagree about eligibility or about absent stock.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from bims_shopify.domain.product import ProductSnapshot

#: Snapshot lifecycle. ``building`` rows are invisible to readers; exactly one
#: snapshot per tenant is ``current``; ``rejected``/``failed`` are metadata
#: only (they never own rows a reader could see).
STATUS_BUILDING = "building"
STATUS_CURRENT = "current"
STATUS_SUPERSEDED = "superseded"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"


def is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "true", "t", "yes")
    return bool(value)


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CatalogRow:
    """One BIMS product row, as reported (eligible or not).

    ``sku`` is the identifier matched against Shopify, resolved at pull time
    from the tenant's ``product_sku_field`` mapping (``code2`` by default).
    """

    bims_id: str
    sku: str | None
    code2: str | None
    name: str
    sell_price: float
    enabled: bool
    exclude_ecommerce: bool
    company_id: int | None

    @classmethod
    def from_raw(cls, raw: dict[str, Any], field_mappings: dict[str, Any]) -> CatalogRow:
        sku_field = field_mappings.get("product_sku_field", "code2")
        sku = raw.get(sku_field)
        code2 = raw.get("code2")
        return cls(
            bims_id="" if raw.get("id") is None else str(raw["id"]),
            sku=str(sku) if sku else None,
            code2=str(code2) if code2 else None,
            name=raw.get("name") or "",
            sell_price=_to_float(raw.get("sell_price")),
            enabled=is_truthy(raw.get("enabled", True)),
            exclude_ecommerce=is_truthy(raw.get("exclude_ecommerce", False)),
            company_id=_to_int(raw.get("company_id")),
        )

    @property
    def eligible(self) -> bool:
        """Enabled, not excluded from e-commerce, and carrying a SKU."""
        return self.enabled and not self.exclude_ecommerce and bool(self.sku)


@dataclass
class StockRecord:
    """Aggregated stock for a SKU plus the per-warehouse quantities behind it.

    ``quantity`` is what BIMS's ``stock_fenicio`` reported for the tenant's
    warehouses together (negatives clamped to 0); ``by_warehouse`` is the same
    figure asked one warehouse at a time, the detail that diagnoses "BIMS shows
    10 in one warehouse, we show 0".
    """

    quantity: float
    by_warehouse: dict[int, float] = field(default_factory=dict)


@dataclass
class CatalogPull:
    """Everything one BIMS pull produced, before it is persisted.

    Mirrors ``PagedPull`` (rows, reported_count, pages, stop_reason, warnings)
    and the stock fetch so nothing is re-derived.
    """

    rows: list[CatalogRow]
    stock: dict[str, float]
    stock_by_warehouse: dict[str, dict[int, float]] = field(default_factory=dict)
    reported_count: int | None = None
    complete: bool | None = None
    pages: int = 0
    stop_reason: str = "empty_page"
    warnings: list[str] = field(default_factory=list)
    #: Per-phase wall-clock seconds: catalog_pull_seconds, stock_fetch_seconds,
    #: warehouse_stock_seconds (None when not fetched).
    timings: dict[str, float | None] = field(default_factory=dict)


@dataclass
class SnapshotMeta:
    id: int
    tenant_id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    rows_pulled: int
    stock_rows: int
    pages: int
    reported_count: int | None
    complete: bool | None
    stop_reason: str
    warnings: list[str]
    timings: dict[str, Any]
    #: Catalog stats computed by the pull (see ``ops.sync_workers``), or None.
    stats: dict[str, Any] | None = None
    #: Why the pull was rejected / failed, when it was.
    rejection: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("started_at", "finished_at"):
            data[key] = data[key].isoformat() if data[key] else None
        return data


@dataclass
class CatalogSnapshot:
    meta: SnapshotMeta
    rows: list[CatalogRow]
    stock: dict[str, StockRecord]


def resolve_product_snapshots(
    rows: list[CatalogRow],
    stock: dict[str, float],
    field_mappings: dict[str, Any],
) -> list[ProductSnapshot]:
    """Eligible rows -> ``ProductSnapshot`` with their stock resolved.

    Safe-by-default: a SKU absent from the stock response has an UNKNOWN stock
    level (``stock=None``), not a zero one. Treating "absent" as "zero" would
    zero out inventory for every SKU the stock endpoint didn't return
    (observed to be ~99% of the catalog in one dry run). An explicit zero is
    produced for absent SKUs only when the tenant opts in via
    ``absent_stock_means_zero``.
    """
    absent_means_zero = is_truthy(field_mappings.get("absent_stock_means_zero", False))
    snapshots: list[ProductSnapshot] = []
    for row in rows:
        sku = row.sku
        if not row.eligible or sku is None:
            continue
        if sku in stock:
            quantity: float | None = stock[sku]
        elif absent_means_zero:
            quantity = 0.0
        else:
            quantity = None
        snapshots.append(
            ProductSnapshot(
                sku=sku, name=row.name, price=row.sell_price, stock=quantity, enabled=True
            )
        )
    return snapshots
