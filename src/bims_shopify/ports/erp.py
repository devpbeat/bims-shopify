"""Port describing operations the application needs from an ERP (BIMS)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from bims_shopify.domain.product import ProductSnapshot
from bims_shopify.domain.sale import SaleOrder, SaleResult
from bims_shopify.domain.snapshot import CatalogPull
from bims_shopify.domain.tenant import Tenant


class ProductLister(Protocol):
    """The only thing the inventory push needs from an ERP: eligible products
    with their stock. Satisfied by the live BIMS adapter and by the reader of
    the persisted snapshot."""

    async def list_products(
        self, tenant: Tenant, since: datetime | None = None
    ) -> list[ProductSnapshot]: ...


class ERPPort(Protocol):
    async def list_products(
        self, tenant: Tenant, since: datetime | None = None
    ) -> list[ProductSnapshot]:
        """Return the current product/stock snapshot known to the ERP.

        When ``since`` is given, only products modified after that timestamp
        are returned (incremental pull); stock is still resolved for all of
        them via a separate warehouse-scoped stock lookup.
        """
        ...

    async def pull_catalog(
        self, tenant: Tenant, since: datetime | None = None, *, warehouse_detail: bool = True
    ) -> CatalogPull:
        """Pull every product row plus aggregated and per-warehouse stock.

        What the PULL worker persists as the catalog snapshot. Unlike
        ``list_products`` it keeps rows that are not eligible and does not
        resolve absent stock; that is done when the snapshot is read.
        """
        ...

    async def create_or_update_sale(self, tenant: Tenant, sale: SaleOrder) -> SaleResult:
        """Push a sale (create or idempotent update) to the ERP."""
        ...

    async def verify_sale_synced(self, tenant: Tenant, sale: SaleOrder) -> dict[str, Any]:
        """Reconcile an unconfirmed sale write via verify_synced."""
        ...

    async def create_purchase_order(
        self, tenant: Tenant, contact_id: int, lines: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Create a purchase order for restocking."""
        ...

    async def request_restocking(self, tenant: Tenant, payload: dict[str, Any]) -> dict[str, Any]:
        """Trigger a generic restocking request."""
        ...

    async def update_stock(self, tenant: Tenant, payload: dict[str, Any]) -> dict[str, Any]:
        """Write a stock adjustment using the tenant's configured field mapping."""
        ...
