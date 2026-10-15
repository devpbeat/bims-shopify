"""Shared fixtures-as-functions for the pull/push worker tests.

``shopify_world`` is a fake Shopify (variant listing + inventory writes) plus
BIMS routes that COUNT their calls, so a test can prove a worker never touched
BIMS. ``land_snapshot`` writes a snapshot straight into the database, the way a
finished pull would have.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import respx

from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.domain.snapshot import CatalogPull, CatalogRow

GRAPHQL = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
BIMS_INDEX = "https://bims.example.com/api/products/index.json"
BIMS_STOCK = "https://bims.example.com/api/products_stocks/stock_fenicio.json"
BIMS_DELETED = "https://bims.example.com/api/products/deleted.json"


def variants_response(*skus: str) -> dict:
    return {
        "data": {
            "productVariants": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": f"gid://shopify/ProductVariant/{i}",
                        "sku": sku,
                        "product": {"id": f"gid://shopify/Product/{i}", "title": sku},
                        "inventoryItem": {"id": f"gid://shopify/InventoryItem/{i}", "tracked": True},
                    }
                    for i, sku in enumerate(skus, start=1)
                ],
            }
        }
    }


class ShopifyWorld:
    """Fake Shopify. ``pushed`` maps SKU -> the last quantity written for it."""

    def __init__(self, skus: list[str], *, fail_writes: bool = False) -> None:
        self.skus = list(skus)
        self.fail_writes = fail_writes
        self.write_calls = 0
        self.listing_calls = 0
        self.pushed: dict[str, int] = {}
        self.mock = respx.mock(assert_all_called=False)
        self.bims_index = self.mock.get(BIMS_INDEX).mock(return_value=httpx.Response(500))
        self.bims_stock = self.mock.post(BIMS_STOCK).mock(return_value=httpx.Response(500))
        self.bims_deleted = self.mock.get(BIMS_DELETED).mock(return_value=httpx.Response(500))
        self.mock.post(GRAPHQL).mock(side_effect=self._graphql)

    def _graphql(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "inventorySetQuantities" in body["query"]:
            self.write_calls += 1
            if self.fail_writes:
                return httpx.Response(500, json={"errors": "shopify down"})
            by_item = {f"gid://shopify/InventoryItem/{i}": s for i, s in enumerate(self.skus, start=1)}
            for q in body["variables"]["input"]["quantities"]:
                self.pushed[by_item[q["inventoryItemId"]]] = q["quantity"]
            return httpx.Response(
                200, json={"data": {"inventorySetQuantities": {"userErrors": []}}}
            )
        self.listing_calls += 1
        return httpx.Response(200, json=variants_response(*self.skus))

    @property
    def bims_calls(self) -> int:
        return self.bims_index.call_count + self.bims_stock.call_count + self.bims_deleted.call_count

    def __enter__(self):
        self.mock.start()
        return self

    def __exit__(self, *exc):
        self.mock.stop()


async def land_snapshot(
    session_factory,
    tenant,
    stock: dict[str, float],
    *,
    no_stock: tuple[str, ...] = (),
    finished_at: datetime | None = None,
    complete: bool | None = True,
    extra_rows: list[CatalogRow] | None = None,
):
    """Persist a snapshot: a row per SKU in ``stock`` and ``no_stock``; stock
    records only for ``stock`` (a ``no_stock`` SKU is ABSENT from BIMS's stock
    answer: unknown, not zero)."""
    skus = [*stock, *no_stock]
    rows = [
        CatalogRow(str(i), sku, sku, sku, 1000.0, True, False, 1) for i, sku in enumerate(skus, start=1)
    ] + list(extra_rows or [])
    pull = CatalogPull(
        rows=rows,
        stock=dict(stock),
        stock_by_warehouse={sku: {1: qty} for sku, qty in stock.items()},
        reported_count=len(rows) if complete is not None else None,
        complete=complete,
        pages=1,
        timings={"catalog_pull_seconds": 1.0, "stock_fetch_seconds": 2.0},
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        staged = await repo.stage(tenant.id, pull, now)
        return await repo.promote(staged.id, finished_at=finished_at or now)
