"""Tests for ShopifyClient's bounded exponential backoff on throttling."""
from __future__ import annotations

import httpx
import pytest
import respx

from bims_shopify.adapters.shopify.client import ShopifyClient

_ORDER_QUERY_RESULT = {"data": {"order": {"id": "gid://shopify/Order/1"}}}


@pytest.fixture
def shopify_client(tenant):
    url = f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    http_client = httpx.AsyncClient(
        base_url=url,
        headers={"X-Shopify-Access-Token": tenant.shopify_access_token},
    )
    client = ShopifyClient(tenant, http_client=http_client, retry_base_delay=0.001)
    yield client


@respx.mock
async def test_429_then_success_eventually_returns_result(shopify_client, tenant):
    route = respx.post(
        f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    ).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"errors": "throttled"}),
            httpx.Response(200, json=_ORDER_QUERY_RESULT),
        ]
    )
    data = await shopify_client._graphql("query { order { id } }", {})
    assert route.call_count == 2
    assert data == _ORDER_QUERY_RESULT["data"]


@respx.mock
async def test_graphql_throttled_error_is_retried(shopify_client, tenant):
    throttled_body = {
        "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]
    }
    route = respx.post(
        f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    ).mock(
        side_effect=[
            httpx.Response(200, json=throttled_body),
            httpx.Response(200, json=_ORDER_QUERY_RESULT),
        ]
    )
    data = await shopify_client._graphql("query { order { id } }", {})
    assert route.call_count == 2
    assert data == _ORDER_QUERY_RESULT["data"]


@respx.mock
async def test_exhausted_retries_raise_shopify_graphql_error(shopify_client, tenant):
    from bims_shopify.adapters.shopify.client import ShopifyGraphQLError

    route = respx.post(
        f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    ).mock(return_value=httpx.Response(503))
    with pytest.raises(ShopifyGraphQLError):
        await shopify_client._graphql("query { order { id } }", {})
    assert route.call_count == 5
