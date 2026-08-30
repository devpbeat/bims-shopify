"""Opt-in live smoke test against the real BIMS tenant "mystore".

Read-only: only calls GET products/index.json and POST
products_stocks/stock_fenicio.json (a read-lookup, not a write). Skipped
unless BIMS_LIVE=1 and BIMS_API_KEY are both set, to keep the default
`pytest` run offline and safe.
"""
from __future__ import annotations

import os

import httpx
import pytest

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter
from bims_shopify.domain.tenant import Tenant

pytestmark = pytest.mark.skipif(
    os.environ.get("BIMS_LIVE") != "1" or not os.environ.get("BIMS_API_KEY"),
    reason="live BIMS smoke test requires BIMS_LIVE=1 and BIMS_API_KEY",
)


def _live_tenant() -> Tenant:
    return Tenant(
        id=1,
        slug="mystore",
        bims_base_url="https://in.bims.app",
        bims_api_key=os.environ["BIMS_API_KEY"],
        shopify_shop_domain="mystore.myshopify.com",
        shopify_access_token="unused",
        shopify_webhook_secret="unused",
        shopify_location_id="unused",
        bims_posale_id=13,
        bims_warehouse_id=25,
        bims_company_id=6,
        bims_currency_id=3,
        bims_payment_method_id=1,
        default_customer_contact_id=1,
        field_mappings={},
    )


async def test_live_products_have_non_null_skus_and_stock_resolves():
    tenant = _live_tenant()
    http_client = httpx.AsyncClient(
        base_url=tenant.bims_base_url,
        headers={
            "Authorization": tenant.bims_auth_header,
            "Accept": "application/json",
        },
        timeout=30.0,
    )
    client = BIMSClient(tenant, http_client=http_client)
    try:
        response = await client.list_products(mode="full", limit=20, company_id=tenant.bims_company_id)
        items = response.get("data") or []
        assert items, "expected at least one product from mystore"
        skus = [entry.get("Product", entry).get("code2") for entry in items]
        assert any(skus), "expected at least one non-null code2 SKU"

        adapter = BIMSERPAdapter(client)
        real_skus = [s for s in skus if s][:5]
        stock = await adapter.fetch_stock_levels(tenant, real_skus)
        assert stock, "expected stock_fenicio to resolve at least one SKU"
        assert all(isinstance(v, float) for v in stock.values())
    finally:
        await client.aclose()
