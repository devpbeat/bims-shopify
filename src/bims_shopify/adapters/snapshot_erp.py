"""ProductLister backed by the persisted catalog snapshot (never calls BIMS)."""
from __future__ import annotations

from datetime import datetime

from bims_shopify.domain.product import ProductSnapshot
from bims_shopify.domain.snapshot import CatalogSnapshot, resolve_product_snapshots
from bims_shopify.domain.tenant import Tenant


class SnapshotERPAdapter:
    """Serves the products of one loaded snapshot to ``SyncInventoryToShopify``.

    Eligibility and the absent-stock rule come from ``resolve_product_snapshots``,
    the same function the live adapter uses: a SKU with no stock record is
    UNKNOWN (``stock=None``) and is never pushed as zero.

    ``since`` is accepted for protocol compatibility and ignored: a snapshot is
    always the whole catalog, so the push always compares everything.
    """

    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot

    async def list_products(
        self, tenant: Tenant, since: datetime | None = None
    ) -> list[ProductSnapshot]:
        stock = {sku: record.quantity for sku, record in self._snapshot.stock.items()}
        return resolve_product_snapshots(self._snapshot.rows, stock, tenant.field_mappings)
