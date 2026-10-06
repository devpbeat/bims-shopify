"""Tests for ShopifyClient's publish-support surface: image/publication fields on
``iter_all_products_with_skus``, ``fetch_publications``, and ``publish_product``.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from bims_shopify.adapters.shopify.client import ShopifyClient, ShopifyGraphQLError


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
async def test_iter_all_products_with_skus_includes_media_and_publication_fields(
    shopify_client, tenant
):
    body = {
        "data": {
            "products": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": "gid://shopify/Product/1",
                        "title": "Shirt",
                        "status": "DRAFT",
                        "mediaCount": {"count": 2},
                        "resourcePublicationsV2": {
                            "nodes": [
                                {
                                    "publication": {"id": "gid://shopify/Publication/1"},
                                    "isPublished": True,
                                },
                                {
                                    "publication": {"id": "gid://shopify/Publication/9"},
                                    "isPublished": False,
                                },
                            ]
                        },
                        "variants": {"nodes": [{"sku": "SKU-A"}]},
                    },
                    {
                        "id": "gid://shopify/Product/2",
                        "title": "No Media",
                        "status": "DRAFT",
                        "mediaCount": {"count": 0},
                        "resourcePublicationsV2": {"nodes": []},
                        "variants": {"nodes": [{"sku": "SKU-B"}]},
                    },
                ],
            }
        }
    }
    respx.post(
        f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    ).mock(return_value=httpx.Response(200, json=body))

    nodes = [node async for node in shopify_client.iter_all_products_with_skus()]

    assert nodes[0]["media_count"] == 2
    assert nodes[0]["published_publication_ids"] == ["gid://shopify/Publication/1"]
    assert nodes[1]["media_count"] == 0
    assert nodes[1]["published_publication_ids"] == []


@respx.mock
async def test_fetch_publications_returns_nodes(shopify_client, tenant):
    body = {
        "data": {
            "publications": {
                "nodes": [
                    {"id": "gid://shopify/Publication/1", "name": "Online Store"},
                    {"id": "gid://shopify/Publication/9", "name": "Point of Sale"},
                ]
            }
        }
    }
    respx.post(
        f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    ).mock(return_value=httpx.Response(200, json=body))

    publications = await shopify_client.fetch_publications()
    assert publications == body["data"]["publications"]["nodes"]


@respx.mock
async def test_publish_product_sends_publication_ids_and_checks_user_errors(
    shopify_client, tenant
):
    route = respx.post(
        f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"publishablePublish": {"userErrors": []}}}
        )
    )

    await shopify_client.publish_product(
        "gid://shopify/Product/1", ["gid://shopify/Publication/1"]
    )

    sent = route.calls[0].request.content
    assert b"gid://shopify/Product/1" in sent
    assert b"gid://shopify/Publication/1" in sent


@respx.mock
async def test_publish_product_raises_on_user_errors(shopify_client, tenant):
    respx.post(
        f"https://{tenant.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "publishablePublish": {
                        "userErrors": [{"field": ["input"], "message": "nope"}]
                    }
                }
            },
        )
    )

    with pytest.raises(ShopifyGraphQLError):
        await shopify_client.publish_product(
            "gid://shopify/Product/1", ["gid://shopify/Publication/1"]
        )
