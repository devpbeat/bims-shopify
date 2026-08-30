"""Port describing operations the application needs from a storefront (Shopify)."""
from __future__ import annotations

from typing import Any, Protocol

from bims_shopify.domain.product import InventoryDelta
from bims_shopify.domain.tenant import Tenant


class StorefrontPort(Protocol):
    async def find_variant_by_sku(self, tenant: Tenant, sku: str) -> dict[str, Any] | None:
        """Look up a product variant (and its inventory item id) by SKU."""
        ...

    async def set_inventory_quantities(
        self, tenant: Tenant, deltas: list[InventoryDelta]
    ) -> None:
        """Push absolute inventory quantities for a batch of SKUs."""
        ...

    async def upsert_product(self, tenant: Tenant, sku: str, name: str, price: float) -> None:
        """Create or update a product/variant to match ERP data."""
        ...

    async def mark_order_as_paid(self, tenant: Tenant, order_id: str) -> None:
        """Mark a Shopify order as paid via orderMarkAsPaid."""
        ...

    async def get_order(self, tenant: Tenant, order_id: str) -> dict[str, Any] | None:
        """Fetch an order by id."""
        ...
