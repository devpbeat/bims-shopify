"""A stateful fake Shopify for the self-healing cycle tests.

Unlike ``sync_world.ShopifyWorld`` (a fixed SKU list), ``HealShopify`` keeps
real products: ``productSet`` and ``productVariantsBulkCreate`` change what the
next variant listing returns, drafts can be published, and every call is
counted per kind so a test can prove that a cycle with nothing to do touched
Shopify zero times.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import respx

from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.domain.snapshot import CatalogPull, CatalogRow
from tests.sync_world import GRAPHQL

PUBLICATION_ID = "gid://shopify/Publication/1"


def product(
    pid: int,
    title: str,
    variants: dict[str, str | None],
    *,
    status: str = "ACTIVE",
    media: int = 1,
    published: bool = True,
    option: str = "Size",
) -> dict[str, Any]:
    """``variants`` maps SKU -> option value (the size, or None for 'Default Title')."""
    return {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "status": status,
        "media": media,
        "published": published,
        "option": option,
        "variants": [{"sku": sku, "size": size} for sku, size in variants.items()],
    }


class HealShopify:
    def __init__(self, products: list[dict[str, Any]] | None = None) -> None:
        self.products: list[dict[str, Any]] = [dict(p) for p in products or []]
        self._next_id = 1000
        self.calls: dict[str, int] = {}
        self.product_sets: list[dict[str, Any]] = []
        self.bulk_creates: list[dict[str, Any]] = []
        self.status_updates: list[tuple[str, str]] = []
        self.publishes: list[str] = []
        self.pushed: dict[str, int] = {}
        #: Titles whose productSet answers with a userError.
        self.fail_titles: set[str] = set()
        self.mock = respx.mock(assert_all_called=False)
        self.mock.post(GRAPHQL).mock(side_effect=self._graphql)

    # -- counters -----------------------------------------------------------

    def count(self, kind: str) -> int:
        return self.calls.get(kind, 0)

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    @property
    def write_calls(self) -> int:
        return sum(self.count(k) for k in ("productSet", "bulkCreate", "inventory", "status", "publish"))

    def skus(self) -> set[str]:
        return {v["sku"] for p in self.products for v in p["variants"]}

    def find(self, title: str) -> dict[str, Any]:
        return next(p for p in self.products if p["title"] == title)

    # -- GraphQL ------------------------------------------------------------

    def _hit(self, kind: str) -> None:
        self.calls[kind] = self.calls.get(kind, 0) + 1

    @staticmethod
    def _ok(data: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"data": data})

    def _graphql(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query, variables = body["query"], body["variables"]
        if "productSet(" in query:
            return self._product_set(variables["input"])
        if "productVariantsBulkCreate(" in query:
            return self._bulk_create(variables)
        if "inventorySetQuantities" in query:
            self._hit("inventory")
            for q in variables["input"]["quantities"]:
                self.pushed[q["inventoryItemId"].rsplit("/", 1)[-1]] = q["quantity"]
            return self._ok({"inventorySetQuantities": {"userErrors": []}})
        if "ListAllVariants" in query:
            self._hit("variants_listing")
            return self._variants_listing()
        if "ListAllProductsWithSkus" in query:
            self._hit("products_listing")
            return self._products_listing(variables.get("query"))
        if "Publications" in query:
            self._hit("publications")
            return self._ok({"publications": {"nodes": [{"id": PUBLICATION_ID, "name": "Online Store"}]}})
        if "productUpdate(" in query:
            self._hit("status")
            update = variables["input"]
            self.status_updates.append((update["id"], update["status"]))
            for p in self.products:
                if p["id"] == update["id"]:
                    p["status"] = update["status"]
            return self._ok({"productUpdate": {"product": {"id": update["id"]}, "userErrors": []}})
        if "publishablePublish(" in query:
            self._hit("publish")
            self.publishes.append(variables["id"])
            for p in self.products:
                if p["id"] == variables["id"]:
                    p["published"] = True
            return self._ok({"publishablePublish": {"publishable": {"id": variables["id"]}, "userErrors": []}})
        raise AssertionError(f"unexpected GraphQL call: {query[:80]}")

    def _variants_listing(self) -> httpx.Response:
        nodes = []
        for p in self.products:
            for v in p["variants"]:
                options = [] if v["size"] is None else [{"name": p["option"], "value": v["size"]}]
                if v["size"] is None:
                    options = [{"name": "Title", "value": "Default Title"}]
                nodes.append(
                    {
                        "id": f"gid://shopify/ProductVariant/{v['sku']}",
                        "sku": v["sku"],
                        "product": {"id": p["id"], "title": p["title"]},
                        "inventoryItem": {"id": f"gid://shopify/InventoryItem/{v['sku']}", "tracked": True},
                        "selectedOptions": options,
                    }
                )
        return self._ok(
            {"productVariants": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
        )

    def _products_listing(self, query: str | None) -> httpx.Response:
        wanted = [p for p in self.products if query != "status:draft" or p["status"] == "DRAFT"]
        nodes = [
            {
                "id": p["id"],
                "title": p["title"],
                "status": p["status"],
                "mediaCount": {"count": p["media"]},
                "resourcePublicationsV2": {
                    "nodes": [{"publication": {"id": PUBLICATION_ID}, "isPublished": p["published"]}]
                },
                "variants": {"nodes": [{"sku": v["sku"]} for v in p["variants"]]},
            }
            for p in wanted
        ]
        return self._ok(
            {"products": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
        )

    def _product_set(self, product_input: dict[str, Any]) -> httpx.Response:
        self._hit("productSet")
        self.product_sets.append(product_input)
        title = product_input["title"]
        if title in self.fail_titles:
            return self._ok(
                {"productSet": {"product": None, "userErrors": [{"field": ["title"], "message": "rejected", "code": "X"}]}}
            )
        self._next_id += 1
        created = {
            "id": f"gid://shopify/Product/{self._next_id}",
            "title": title,
            "status": product_input["status"],
            "media": 0,
            "published": False,
            "option": product_input["productOptions"][0]["name"],
            "variants": [
                {
                    "sku": v["sku"],
                    "size": None if v["optionValues"][0]["optionName"] == "Title" else v["optionValues"][0]["name"],
                }
                for v in product_input["variants"]
            ],
        }
        self.products.append(created)
        return self._ok({"productSet": {"product": {"id": created["id"], "title": title}, "userErrors": []}})

    def _bulk_create(self, variables: dict[str, Any]) -> httpx.Response:
        self._hit("bulkCreate")
        self.bulk_creates.append(variables)
        target = next(p for p in self.products if p["id"] == variables["productId"])
        created = []
        for v in variables["variants"]:
            sku = v["inventoryItem"]["sku"]
            target["variants"].append({"sku": sku, "size": v["optionValues"][0]["name"]})
            created.append({"id": f"gid://shopify/ProductVariant/{sku}", "sku": sku})
        return self._ok({"productVariantsBulkCreate": {"productVariants": created, "userErrors": []}})

    # -- context manager ----------------------------------------------------

    def __enter__(self):
        self.mock.start()
        return self

    def __exit__(self, *exc):
        self.mock.stop()


async def land_catalog(
    session_factory,
    tenant,
    items: list[tuple[str, str, float | None]],
    *,
    finished_at: datetime | None = None,
    complete: bool | None = True,
):
    """Persist a snapshot of ``(sku, BIMS name, stock)`` rows; ``stock=None`` means
    the SKU is ABSENT from the stock answer (unknown, not zero)."""
    rows = [
        CatalogRow(str(i), sku, sku, name, 1000.0, True, False, 1)
        for i, (sku, name, _stock) in enumerate(items, start=1)
    ]
    stock = {sku: qty for sku, _name, qty in items if qty is not None}
    pull = CatalogPull(
        rows=rows,
        stock=stock,
        stock_by_warehouse={sku: {1: qty} for sku, qty in stock.items()},
        reported_count=len(rows),
        complete=complete,
        pages=1,
        timings={"catalog_pull_seconds": 1.0, "stock_fetch_seconds": 2.0},
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        staged = await repo.stage(tenant.id, pull, now)
        return await repo.promote(staged.id, finished_at=finished_at or now)
