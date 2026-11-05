"""Domain rules for the "products without photos" view.

A product with no Shopify media shows a grey placeholder in the storefront.
With the image gate off (``publish_require_images = false``) such products can
be live, so the operator needs to know which ones to upload photos for in BIMS.

Everything here is pure: no Shopify, no database.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCOPE_LIVE = "live"
SCOPE_ALL = "all"
SCOPES = (SCOPE_LIVE, SCOPE_ALL)

_PRODUCT_GID_PREFIX = "gid://shopify/Product/"


@dataclass(frozen=True)
class ProductWithoutPhotos:
    """One Shopify product that has zero media, with the facts needed to act in BIMS."""

    product_id: str
    title: str | None
    #: The BIMS SKUs (``code2``) of the product's variants; usually exactly one.
    skus: tuple[str, ...]
    status: str | None
    #: ``None``: the Online Store publication could not be resolved.
    on_online_store: bool | None
    #: The quantity BIMS reported for these SKUs (the figure pushed to Shopify),
    #: summed over the SKUs it knows; ``None`` when it knows none of them.
    stock: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "title": self.title,
            "skus": list(self.skus),
            "status": self.status,
            "on_online_store": self.on_online_store,
            "stock": self.stock,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProductWithoutPhotos:
        return cls(
            product_id=str(data["product_id"]),
            title=data.get("title"),
            skus=tuple(data.get("skus") or ()),
            status=data.get("status"),
            on_online_store=data.get("on_online_store"),
            stock=data.get("stock"),
        )


def is_live(row: ProductWithoutPhotos) -> bool:
    """Whether customers can see the product: ACTIVE and not known to be off the Online Store.

    An unresolved publication (``None``) counts as live: hiding a product that
    may be visible would understate the problem.
    """
    return row.status == "ACTIVE" and row.on_online_store is not False


def filter_rows(
    rows: list[ProductWithoutPhotos], scope: str, query: str
) -> list[ProductWithoutPhotos]:
    """Keep the rows in ``scope`` whose title or SKU contains ``query`` (case-insensitive)."""
    needle = query.strip().lower()
    selected: list[ProductWithoutPhotos] = []
    for row in rows:
        if scope == SCOPE_LIVE and not is_live(row):
            continue
        if needle:
            haystack = [(row.title or "").lower(), *(sku.lower() for sku in row.skus)]
            if not any(needle in text for text in haystack):
                continue
        selected.append(row)
    return selected


def admin_product_url(shop_domain: str, product_id: str) -> str | None:
    """The Shopify admin page of a product, or ``None`` when ``product_id`` is not a product gid."""
    if not product_id.startswith(_PRODUCT_GID_PREFIX):
        return None
    numeric = product_id.removeprefix(_PRODUCT_GID_PREFIX)
    if not numeric.isdigit():
        return None
    return f"https://{shop_domain}/admin/products/{numeric}"
