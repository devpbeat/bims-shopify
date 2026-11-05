"""``missing_photos``: list the Shopify products that have no media (STRICTLY read-only).

The Shopify half is a live catalog walk (the same ``iter_all_products_with_skus``
scan ``diagnose`` and ``publish`` use, which already exposes ``media_count``).
It takes minutes for ~11k products, so it runs as a background job and its
result is stored on the job row for the portal to read. The stock figure comes
from the stored BIMS snapshot, never from a BIMS pull.
"""
from __future__ import annotations

from typing import Any

from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.application.products_without_photos import scan_products_without_photos
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ops.catalog import (
    _TRANSIENT_SHOPIFY_EXCEPTIONS,
    ProgressFn,
    _emit_progress,
    _load_status_snapshot,
    _resolve_online_store_publication_id,
)


async def run_missing_photos(
    tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None
) -> dict[str, Any]:
    await _emit_progress(progress, 0, None, "reading the catalog snapshot")
    snapshot = await _load_status_snapshot(tenant.id)
    stock_by_sku: dict[str, float] = {}
    stock_as_of: str | None = None
    if snapshot is not None:
        stock_by_sku = {sku: record.quantity for sku, record in snapshot.stock.items()}
        finished = snapshot.meta.finished_at or snapshot.meta.started_at
        stock_as_of = finished.isoformat()

    shopify_client = ShopifyClient(tenant)
    try:
        publication_id: str | None = None
        try:
            publication_id = await _resolve_online_store_publication_id(shopify_client)
        except (SystemExit, *_TRANSIENT_SHOPIFY_EXCEPTIONS):
            # Secondary lookup: the rows then say "online store: unknown".
            publication_id = None
        total: int | None = None
        try:
            total = await shopify_client.products_count()
        except _TRANSIENT_SHOPIFY_EXCEPTIONS:
            total = None

        await _emit_progress(progress, 0, total, "scanning Shopify products")
        scan = await scan_products_without_photos(
            shopify_client.iter_all_products_with_skus(),
            publication_id=publication_id,
            stock_by_sku=stock_by_sku,
            total=total,
            progress=progress,
        )
        result = scan.to_dict()
        result["stock_as_of"] = stock_as_of
        return result
    finally:
        await shopify_client.aclose()
