"""Tests for ShopifyClient.set_inventory_quantities' activate-then-retry self-heal."""
from __future__ import annotations

import httpx
import pytest
import respx

from bims_shopify.adapters.shopify.client import ShopifyClient, ShopifyGraphQLError
from bims_shopify.domain.product import InventoryDelta


@pytest.fixture
def shopify_client(tenant):
    url = f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    http_client = httpx.AsyncClient(
        base_url=url,
        headers={"X-Shopify-Access-Token": tenant.shopify_access_token},
    )
    client = ShopifyClient(tenant, http_client=http_client, retry_base_delay=0.001)
    yield client


def _graphql_url(tenant) -> str:
    return f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"


@respx.mock
async def test_item_not_stocked_at_location_activates_then_retries_once(shopify_client, tenant):
    not_stocked_response = {
        "data": {
            "inventorySetQuantities": {
                "inventoryAdjustmentGroup": None,
                "userErrors": [
                    {
                        "field": ["input", "quantities", "0", "inventoryItemId"],
                        "message": "The specified inventory item is not stocked at the location.",
                        "code": "ITEM_NOT_STOCKED_AT_LOCATION",
                    }
                ],
            }
        }
    }
    activate_response = {
        "data": {
            "inventoryActivate": {
                "inventoryLevel": {"id": "gid://shopify/InventoryLevel/1"},
                "userErrors": [],
            }
        }
    }
    success_response = {
        "data": {
            "inventorySetQuantities": {
                "inventoryAdjustmentGroup": {"createdAt": "2026-01-01T00:00:00Z"},
                "userErrors": [],
            }
        }
    }

    route = respx.post(_graphql_url(tenant)).mock(
        side_effect=[
            httpx.Response(200, json=not_stocked_response),
            httpx.Response(200, json=activate_response),
            httpx.Response(200, json=success_response),
        ]
    )

    deltas = [
        InventoryDelta(
            sku="SKU-1",
            previous_stock=0,
            new_stock=5,
            variant_inventory_item_id="gid://shopify/InventoryItem/123",
        )
    ]

    await shopify_client.set_inventory_quantities(tenant, deltas)

    assert route.call_count == 3
    activate_request = route.calls[1].request
    assert b"InventoryActivate" in activate_request.content
    assert b"gid://shopify/InventoryItem/123" in activate_request.content


@respx.mock
async def test_persistent_not_stocked_error_raises_after_single_retry(shopify_client, tenant):
    not_stocked_response = {
        "data": {
            "inventorySetQuantities": {
                "inventoryAdjustmentGroup": None,
                "userErrors": [
                    {
                        "field": ["input", "quantities", "0", "inventoryItemId"],
                        "message": "The specified inventory item is not stocked at the location.",
                        "code": "ITEM_NOT_STOCKED_AT_LOCATION",
                    }
                ],
            }
        }
    }
    activate_response = {
        "data": {"inventoryActivate": {"inventoryLevel": None, "userErrors": []}}
    }

    route = respx.post(_graphql_url(tenant)).mock(
        side_effect=[
            httpx.Response(200, json=not_stocked_response),
            httpx.Response(200, json=activate_response),
            httpx.Response(200, json=not_stocked_response),
        ]
    )

    deltas = [
        InventoryDelta(
            sku="SKU-1",
            previous_stock=0,
            new_stock=5,
            variant_inventory_item_id="gid://shopify/InventoryItem/123",
        )
    ]

    with pytest.raises(ShopifyGraphQLError):
        await shopify_client.set_inventory_quantities(tenant, deltas)

    assert route.call_count == 3
