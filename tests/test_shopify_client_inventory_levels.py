"""ShopifyClient.iter_variant_inventory: every variant with its REAL available
quantity at the tenant's location, and "no level there" kept apart from zero."""
from __future__ import annotations

import json

import httpx
import respx

from bims_shopify.adapters.shopify.client import ShopifyClient


def _url(tenant) -> str:
    return f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"


def _client(tenant) -> ShopifyClient:
    http = httpx.AsyncClient(base_url=_url(tenant), headers={"X-Shopify-Access-Token": "t"})
    return ShopifyClient(tenant, http_client=http, retry_base_delay=0.001)


def _node(n: int, sku: str | None, level: dict | None) -> dict:
    return {
        "id": f"gid://shopify/ProductVariant/{n}",
        "sku": sku,
        "inventoryItem": {"id": f"gid://shopify/InventoryItem/{n}", "inventoryLevel": level},
    }


def _level(quantity: int) -> dict:
    return {"quantities": [{"name": "available", "quantity": quantity}]}


def _page(nodes: list[dict], cursor: str | None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "productVariants": {
                    "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        },
    )


@respx.mock
async def test_yields_the_available_quantity_and_pages_through_every_variant(tenant):
    route = respx.post(_url(tenant)).mock(
        side_effect=[
            _page([_node(1, "A", _level(5))], "c1"),
            _page([_node(2, "B", _level(7))], None),
        ]
    )
    client = _client(tenant)
    rows = [row async for row in client.iter_variant_inventory(tenant)]

    assert rows == [
        {"sku": "A", "inventory_item_id": "gid://shopify/InventoryItem/1", "available": 5},
        {"sku": "B", "inventory_item_id": "gid://shopify/InventoryItem/2", "available": 7},
    ]
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)
    assert second["variables"]["cursor"] == "c1"


@respx.mock
async def test_asks_for_the_tenants_location_and_the_available_name(tenant):
    route = respx.post(_url(tenant)).mock(return_value=_page([], None))
    client = _client(tenant)
    _ = [row async for row in client.iter_variant_inventory(tenant)]

    body = json.loads(route.calls[0].request.content)
    assert body["variables"]["locationId"] == tenant.shopify_location_id
    assert "inventoryLevel(locationId: $locationId)" in body["query"]
    assert 'quantities(names: ["available"])' in body["query"]


@respx.mock
async def test_a_variant_without_a_level_is_none_and_a_stocked_zero_is_zero(tenant):
    respx.post(_url(tenant)).mock(
        return_value=_page([_node(1, "NOLEVEL", None), _node(2, "ZERO", _level(0))], None)
    )
    client = _client(tenant)
    rows = {row["sku"]: row["available"] async for row in client.iter_variant_inventory(tenant)}

    assert rows["NOLEVEL"] is None
    assert rows["ZERO"] == 0
    assert rows["ZERO"] is not None


@respx.mock
async def test_a_level_without_an_available_entry_is_not_a_zero(tenant):
    respx.post(_url(tenant)).mock(
        return_value=_page([_node(1, "ODD", {"quantities": []})], None)
    )
    client = _client(tenant)
    rows = [row async for row in client.iter_variant_inventory(tenant)]
    assert rows[0]["available"] is None


@respx.mock
async def test_blank_skus_are_skipped_and_skus_are_stripped(tenant):
    respx.post(_url(tenant)).mock(
        return_value=_page(
            [_node(1, None, _level(1)), _node(2, "  ", _level(1)), _node(3, " X ", _level(4))],
            None,
        )
    )
    client = _client(tenant)
    rows = [row async for row in client.iter_variant_inventory(tenant)]
    assert [r["sku"] for r in rows] == ["X"]
