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


def inventory_response(skus: list[str], levels: dict[str, int | None]) -> dict:
    """The ``ListVariantInventory`` answer: ``levels[sku]`` is the available
    quantity at the location, or ``None`` (also: SKU absent) for NO level there."""
    nodes = []
    for i, sku in enumerate(skus, start=1):
        quantity = levels.get(sku)
        level = (
            None
            if quantity is None
            else {"quantities": [{"name": "available", "quantity": quantity}]}
        )
        nodes.append(
            {
                "id": f"gid://shopify/ProductVariant/{i}",
                "sku": sku,
                "inventoryItem": {"id": f"gid://shopify/InventoryItem/{i}", "inventoryLevel": level},
            }
        )
    return {
        "data": {
            "productVariants": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes,
            }
        }
    }


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
    """Fake Shopify. ``pushed`` maps SKU -> the last quantity written for it.

    ``levels`` is what Shopify REALLY holds per SKU (``None``/absent: no
    inventory level at the location). Writes update it, unless
    ``ignore_writes`` (a mutation accepted but never applied)."""

    def __init__(
        self,
        skus: list[str],
        *,
        fail_writes: bool = False,
        levels: dict[str, int | None] | None = None,
        ignore_writes: bool = False,
    ) -> None:
        self.skus = list(skus)
        self.fail_writes = fail_writes
        self.levels: dict[str, int | None] = dict(levels or {})
        self.ignore_writes = ignore_writes
        self.write_calls = 0
        self.listing_calls = 0
        self.inventory_reads = 0
        #: Called after every inventory read (e.g. to move a fake clock).
        self.after_inventory_read = None
        self.pushed: dict[str, int] = {}
        #: SKU -> stock BIMS serves, or None while BIMS is "down" (HTTP 500).
        self.bims_stock_by_sku: dict[str, float] | None = None
        self.mock = respx.mock(assert_all_called=False)
        #: Every request that reached a BIMS route, counted by OUR handlers:
        #: respx wipes its own call counters when the context exits.
        self.bims_requests: list[str] = []
        self.mock.get(BIMS_INDEX).mock(side_effect=self._bims_index)
        self.mock.post(BIMS_STOCK).mock(side_effect=self._bims_stock)
        self.mock.get(BIMS_DELETED).mock(side_effect=self._bims_deleted)
        self.mock.post(GRAPHQL).mock(side_effect=self._graphql)

    def serve_bims(self, stock: dict[str, float] | None) -> None:
        """Make BIMS answer with one product per SKU and this stock (None: down)."""
        self.bims_stock_by_sku = stock

    def _bims_deleted(self, request: httpx.Request) -> httpx.Response:
        self.bims_requests.append("deleted")
        return httpx.Response(500)

    def _bims_index(self, request: httpx.Request) -> httpx.Response:
        self.bims_requests.append("index")
        if self.bims_stock_by_sku is None:
            return httpx.Response(500)
        skus = list(self.bims_stock_by_sku)
        offset = int(request.url.params.get("offset", 0))
        data = (
            [
                {"Product": {"id": i, "name": s, "code2": s, "sell_price": "1000",
                             "enabled": True, "exclude_ecommerce": False}}
                for i, s in enumerate(skus, start=1)
            ]
            if offset == 0
            else []
        )
        return httpx.Response(200, json={"status": "ok", "count": str(len(skus)), "data": data})

    def _bims_stock(self, request: httpx.Request) -> httpx.Response:
        self.bims_requests.append("stock")
        if self.bims_stock_by_sku is None:
            return httpx.Response(500)
        wanted = set(json.loads(request.content)["skus"])
        rows = [{"sku": s, "stock": q} for s, q in self.bims_stock_by_sku.items() if s in wanted]
        return httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": rows}})

    def _graphql(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "inventorySetQuantities" in body["query"]:
            self.write_calls += 1
            if self.fail_writes:
                return httpx.Response(500, json={"errors": "shopify down"})
            by_item = {f"gid://shopify/InventoryItem/{i}": s for i, s in enumerate(self.skus, start=1)}
            for q in body["variables"]["input"]["quantities"]:
                sku = by_item[q["inventoryItemId"]]
                self.pushed[sku] = q["quantity"]
                if not self.ignore_writes:
                    self.levels[sku] = q["quantity"]
            return httpx.Response(
                200, json={"data": {"inventorySetQuantities": {"userErrors": []}}}
            )
        if "ListVariantInventory" in body["query"]:
            self.inventory_reads += 1
            response = httpx.Response(200, json=inventory_response(self.skus, self.levels))
            if self.after_inventory_read is not None:
                self.after_inventory_read()
            return response
        self.listing_calls += 1
        return httpx.Response(200, json=variants_response(*self.skus))

    @property
    def bims_calls(self) -> int:
        return len(self.bims_requests)

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
