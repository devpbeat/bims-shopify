"""Find the Shopify products that have no photos, and shape them for the portal.

The scan is the slow half (a full Shopify catalog walk, minutes for ~11k
products), so it runs as a background ops job; its result is stored on the job
row and ``build_view`` serves filtered, paginated slices of it cheaply.
"""
from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bims_shopify.domain.products_without_photos import (
    ProductWithoutPhotos,
    admin_product_url,
    filter_rows,
    is_live,
)

#: Hard cap on the rows kept (and stored) per scan, so a catalog full of
#: photo-less products cannot exhaust memory or bloat the job row. The counts
#: stay exact beyond it; ``truncated`` tells the operator the list is partial.
MAX_STORED_ROWS = 5000
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

ProgressFn = Callable[[int, int | None, str], "Awaitable[None] | None"]
_PROGRESS_EVERY = 100


@dataclass
class PhotoScan:
    rows: list[ProductWithoutPhotos] = field(default_factory=list)
    scanned: int = 0
    without_photos: int = 0
    live_without_photos: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.to_dict() for row in self.rows],
            "scanned": self.scanned,
            "without_photos": self.without_photos,
            "live_without_photos": self.live_without_photos,
            "truncated": self.truncated,
        }


def _stock_for(skus: list[str], stock_by_sku: dict[str, float]) -> float | None:
    known = [stock_by_sku[sku] for sku in skus if sku in stock_by_sku]
    return sum(known) if known else None


async def scan_products_without_photos(
    products: AsyncIterable[dict[str, Any]],
    *,
    publication_id: str | None,
    stock_by_sku: dict[str, float],
    row_cap: int = MAX_STORED_ROWS,
    total: int | None = None,
    progress: ProgressFn | None = None,
) -> PhotoScan:
    """Stream ``products`` (as ``iter_all_products_with_skus`` yields them), keeping
    only those with no media. Only the photo-less ones are held in memory."""
    scan = PhotoScan()
    async for product in products:
        scan.scanned += 1
        if (product.get("media_count") or 0) <= 0:
            published = list(product.get("published_publication_ids") or [])
            skus = [s for s in (product.get("skus") or []) if s]
            row = ProductWithoutPhotos(
                product_id=str(product["id"]),
                title=product.get("title"),
                skus=tuple(skus),
                status=product.get("status"),
                on_online_store=None if publication_id is None else publication_id in published,
                stock=_stock_for(skus, stock_by_sku),
            )
            scan.without_photos += 1
            if is_live(row):
                scan.live_without_photos += 1
            if len(scan.rows) < row_cap:
                scan.rows.append(row)
            else:
                scan.truncated = True
        if progress is not None and scan.scanned % _PROGRESS_EVERY == 0:
            await _emit(progress, scan.scanned, total, "scanning Shopify products")
    if progress is not None:
        await _emit(progress, scan.scanned, total, "scanning Shopify products")
    return scan


async def _emit(progress: ProgressFn, done: int, total: int | None, message: str) -> None:
    outcome = progress(done, total, message)
    if inspect.isawaitable(outcome):
        await outcome


def build_view(
    stored: dict[str, Any],
    *,
    scope: str,
    query: str,
    page: int,
    page_size: int,
    shop_domain: str,
) -> dict[str, Any]:
    """One page of a stored scan result, with the admin link added to each row.

    Only the documented fields are exposed: whatever else the stored dict
    holds is rebuilt through ``ProductWithoutPhotos`` and dropped.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    rows = [ProductWithoutPhotos.from_dict(raw) for raw in stored.get("rows") or []]
    selected = filter_rows(rows, scope, query)
    start = (page - 1) * page_size
    items = [
        {**row.to_dict(), "admin_url": admin_product_url(shop_domain, row.product_id)}
        for row in selected[start : start + page_size]
    ]
    return {
        "scope": scope,
        "page": page,
        "page_size": page_size,
        "total": len(selected),
        "items": items,
        "counts": {
            "live": stored.get("live_without_photos", 0),
            "all": stored.get("without_photos", 0),
            "scanned": stored.get("scanned", 0),
        },
        "truncated": bool(stored.get("truncated", False)),
    }
