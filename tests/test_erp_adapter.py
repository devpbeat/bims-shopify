"""Unit tests for BIMSERPAdapter.list_products: pagination, filtering, stock merge."""
from __future__ import annotations

from datetime import UTC

import httpx
import pytest
import respx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter


def _product(**overrides):
    base = {
        "id": 1,
        "name": "Test product",
        "code2": "SKU-1",
        "sell_price": "1000",
        "enabled": True,
        "exclude_ecommerce": False,
        "company_id": "6",
    }
    base.update(overrides)
    return {"Product": base}


@pytest.fixture
def bims_client(tenant):
    tenant.bims_company_id = 6
    tenant.bims_warehouse_id = 25
    http_client = httpx.AsyncClient(
        base_url=tenant.bims_base_url,
        headers={"Authorization": tenant.bims_auth_header},
    )
    client = BIMSClient(tenant, http_client=http_client)
    yield client


@respx.mock
async def test_pagination_exhausts_all_pages(bims_client, tenant):
    page1 = [_product(id=i, code2=f"SKU-{i}") for i in range(250)]
    page2 = [_product(id=250, code2="SKU-250")]

    route = respx.get(f"{tenant.bims_base_url}/api/products/index.json")
    route.side_effect = [
        httpx.Response(200, json={"status": "ok", "count": "251", "data": page1}),
        httpx.Response(200, json={"status": "ok", "count": "251", "data": page2}),
        httpx.Response(200, json={"status": "ok", "count": "251", "data": []}),
    ]
    respx.post(f"{tenant.bims_base_url}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "data": {"stockPorSku": [{"sku": f"SKU-{i}", "stock": 1} for i in range(251)]},
            },
        )
    )

    adapter = BIMSERPAdapter(bims_client)
    snapshots = await adapter.list_products(tenant)

    assert len(snapshots) == 251
    assert {s.sku for s in snapshots} == {f"SKU-{i}" for i in range(251)}


@respx.mock
async def test_filters_disabled_and_excluded_and_wrong_company(bims_client, tenant):
    data = [
        _product(id=1, code2="OK-1", enabled=True, exclude_ecommerce=False, company_id="6"),
        _product(id=2, code2="DISABLED", enabled=False),
        _product(id=3, code2="EXCLUDED", exclude_ecommerce=True),
        _product(id=4, code2="OTHER-CO", company_id="7"),
        _product(id=5, code2=None),
    ]
    respx.get(f"{tenant.bims_base_url}/api/products/index.json").mock(
        side_effect=[
            httpx.Response(200, json={"status": "ok", "count": "5", "data": data}),
            httpx.Response(200, json={"status": "ok", "count": "5", "data": []}),
        ]
    )
    respx.post(f"{tenant.bims_base_url}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(
            200, json={"status": "OK", "data": {"stockPorSku": [{"sku": "OK-1", "stock": 5}]}}
        )
    )

    adapter = BIMSERPAdapter(bims_client)
    snapshots = await adapter.list_products(tenant)

    assert [s.sku for s in snapshots] == ["OK-1"]
    assert snapshots[0].stock == 5


@respx.mock
async def test_incremental_since_is_passed_as_last_update(bims_client, tenant):
    from datetime import datetime

    route = respx.get(f"{tenant.bims_base_url}/api/products/index.json").mock(
        side_effect=[
            httpx.Response(200, json={"status": "ok", "count": "0", "data": []}),
        ]
    )
    respx.post(f"{tenant.bims_base_url}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": []}})
    )

    adapter = BIMSERPAdapter(bims_client)
    since = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    await adapter.list_products(tenant, since=since)

    request = route.calls.last.request
    assert "last_update=2026-01-01" in str(request.url)


@respx.mock
async def test_since_is_formatted_verbatim_no_timezone_conversion(bims_client, tenant):
    """Documents the explicit assumption: `since` is forwarded to BIMS as
    naive local wall-clock text with no UTC conversion applied, even though
    our own persisted sync state is UTC (see LAST_UPDATE_FORMAT docstring)."""
    from datetime import datetime

    route = respx.get(f"{tenant.bims_base_url}/api/products/index.json").mock(
        side_effect=[httpx.Response(200, json={"status": "ok", "count": "0", "data": []})]
    )
    respx.post(f"{tenant.bims_base_url}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": []}})
    )

    adapter = BIMSERPAdapter(bims_client)
    since_utc = datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC)
    await adapter.list_products(tenant, since=since_utc)

    request = route.calls.last.request
    # No timezone offset is appended; the UTC wall-clock value is sent as-is.
    assert "last_update=2026-01-01+12%3A30%3A45" in str(request.url)


@respx.mock
async def test_stock_lookup_scoped_to_tenant_warehouse(bims_client, tenant):
    respx.get(f"{tenant.bims_base_url}/api/products/index.json").mock(
        side_effect=[
            httpx.Response(
                200, json={"status": "ok", "count": "1", "data": [_product(id=1, code2="SKU-1")]}
            ),
            httpx.Response(200, json={"status": "ok", "count": "1", "data": []}),
        ]
    )
    stock_route = respx.post(
        f"{tenant.bims_base_url}/api/products_stocks/stock_fenicio.json"
    ).mock(return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": [{"sku": "SKU-1", "stock": 7}]}}))

    adapter = BIMSERPAdapter(bims_client)
    snapshots = await adapter.list_products(tenant)

    body = stock_route.calls.last.request.content
    import json

    parsed = json.loads(body)
    assert parsed["warehouse_ids"] == [25]
    assert snapshots[0].stock == 7


@respx.mock
async def test_missing_stock_is_none_not_zero_by_default(bims_client, tenant):
    """SKUs absent from stock_fenicio's response must NOT be treated as zero stock.

    Regression test for the bug where ~99% of a live store's inventory would
    be zeroed out because SKUs missing from the stock response silently
    defaulted to 0.0. Absent SKUs must resolve to ``None`` (unknown stock,
    do not touch) unless the tenant explicitly opts in via
    ``field_mappings['absent_stock_means_zero'] = True``.
    """
    respx.get(f"{tenant.bims_base_url}/api/products/index.json").mock(
        side_effect=[
            httpx.Response(
                200, json={"status": "ok", "count": "1", "data": [_product(id=1, code2="NO-STOCK")]}
            ),
            httpx.Response(200, json={"status": "ok", "count": "1", "data": []}),
        ]
    )
    respx.post(f"{tenant.bims_base_url}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": []}})
    )

    adapter = BIMSERPAdapter(bims_client)
    snapshots = await adapter.list_products(tenant)

    assert snapshots[0].stock is None


@respx.mock
async def test_missing_stock_is_zero_when_tenant_opts_in(bims_client, tenant):
    tenant.field_mappings = {"absent_stock_means_zero": True}
    respx.get(f"{tenant.bims_base_url}/api/products/index.json").mock(
        side_effect=[
            httpx.Response(
                200, json={"status": "ok", "count": "1", "data": [_product(id=1, code2="NO-STOCK")]}
            ),
            httpx.Response(200, json={"status": "ok", "count": "1", "data": []}),
        ]
    )
    respx.post(f"{tenant.bims_base_url}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": []}})
    )

    adapter = BIMSERPAdapter(bims_client)
    snapshots = await adapter.list_products(tenant)

    assert snapshots[0].stock == 0.0

@respx.mock
async def test_list_deleted_product_ids_parses_response(bims_client, tenant):
    from datetime import datetime

    route = respx.get(f"{tenant.bims_base_url}/api/products/deleted.json").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "code": "200", "message": None, "data": [{"id": 1}, {"id": 2}]},
        )
    )

    adapter = BIMSERPAdapter(bims_client)
    since = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    ids = await adapter.list_deleted_product_ids(tenant, since)

    assert ids == ["1", "2"]
    assert "last_update=2026-01-01" in str(route.calls.last.request.url)


@respx.mock
async def test_list_deleted_product_ids_empty_when_no_data(bims_client, tenant):
    from datetime import datetime

    respx.get(f"{tenant.bims_base_url}/api/products/deleted.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "code": "200", "message": None, "data": None})
    )

    adapter = BIMSERPAdapter(bims_client)
    ids = await adapter.list_deleted_product_ids(tenant, datetime(2026, 1, 1, tzinfo=UTC))

    assert ids == []
