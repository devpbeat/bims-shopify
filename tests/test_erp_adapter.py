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
async def test_since_is_converted_to_tenant_bims_local_time(bims_client, tenant):
    """`since` (aware UTC) must be converted to the tenant's BIMS-local wall
    clock time (naive, no offset) before being sent as `last_update`, per
    `adapters.bims.timezones.to_bims_local`. America/Asuncion is UTC-3."""
    from datetime import datetime

    tenant.bims_timezone = "America/Asuncion"
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
    # 12:30:45 UTC -> 09:30:45 America/Asuncion (UTC-3), no offset suffix.
    assert "last_update=2026-01-01+09%3A30%3A45" in str(request.url)


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


@respx.mock
async def test_stock_lookup_aggregates_across_bims_warehouse_ids(bims_client, tenant):
    """When `bims_warehouse_ids` is set, stock_fenicio must be called with
    that full list instead of just `bims_warehouse_id`."""
    tenant.bims_warehouse_ids = [24, 25, 26, 27]
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
    ).mock(return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": [{"sku": "SKU-1", "stock": 12}]}}))

    adapter = BIMSERPAdapter(bims_client)
    snapshots = await adapter.list_products(tenant)

    import json

    parsed = json.loads(stock_route.calls.last.request.content)
    assert parsed["warehouse_ids"] == [24, 25, 26, 27]
    assert snapshots[0].stock == 12


@respx.mock
async def test_stock_lookup_falls_back_to_bims_warehouse_id_when_ids_empty(bims_client, tenant):
    """`bims_warehouse_ids` empty (the default) must fall back to the single
    `bims_warehouse_id`, preserving existing single-warehouse behavior."""
    assert tenant.bims_warehouse_ids == []
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
    ).mock(return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": [{"sku": "SKU-1", "stock": 3}]}}))

    adapter = BIMSERPAdapter(bims_client)
    await adapter.list_products(tenant)

    import json

    parsed = json.loads(stock_route.calls.last.request.content)
    assert parsed["warehouse_ids"] == [25]


@respx.mock
async def test_negative_aggregated_stock_is_clamped_to_zero_and_logged(bims_client, tenant):
    """Aggregated stock across multiple warehouses can go negative (observed
    live: -1). Merchants must never see negative Shopify availability, so
    the adapter clamps to 0 and logs the count of clamped SKUs."""
    import structlog

    tenant.bims_warehouse_ids = [24, 25, 26, 27]
    respx.get(f"{tenant.bims_base_url}/api/products/index.json").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "status": "ok",
                    "count": "2",
                    "data": [_product(id=1, code2="NEG-1"), _product(id=2, code2="OK-2")],
                },
            ),
            httpx.Response(200, json={"status": "ok", "count": "2", "data": []}),
        ]
    )
    respx.post(f"{tenant.bims_base_url}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "data": {
                    "stockPorSku": [
                        {"sku": "NEG-1", "stock": -1},
                        {"sku": "OK-2", "stock": 4},
                    ]
                },
            },
        )
    )

    adapter = BIMSERPAdapter(bims_client)
    with structlog.testing.capture_logs() as captured:
        snapshots = await adapter.list_products(tenant)

    by_sku = {s.sku: s.stock for s in snapshots}
    assert by_sku["NEG-1"] == 0.0
    assert by_sku["OK-2"] == 4.0
    warnings = [entry for entry in captured if entry.get("event") == "negative_stock_clamped"]
    assert len(warnings) == 1
    assert warnings[0]["clamped_count"] == 1
    assert warnings[0]["tenant"] == "acme"
