"""Product/inventory snapshot entities used for diffing BIMS state against Shopify."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductSnapshot:
    """A minimal, comparable view of a BIMS product used for sync diffing."""

    sku: str
    name: str
    price: float
    stock: float | None
    """Current stock, or ``None`` if the SKU was absent from the ERP's stock
    response (unknown stock — must not be treated as zero)."""
    enabled: bool = True


@dataclass(frozen=True)
class InventoryDelta:
    """A single SKU whose stock quantity changed between two snapshots."""

    sku: str
    previous_stock: float | None
    new_stock: float
    variant_inventory_item_id: str | None = None

    @property
    def delta(self) -> float:
        if self.previous_stock is None:
            return self.new_stock
        return self.new_stock - self.previous_stock
