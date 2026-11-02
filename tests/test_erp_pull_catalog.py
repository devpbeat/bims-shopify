"""BIMSERPAdapter.pull_catalog: the raw material of the persisted snapshot.

It reuses the existing pager and stock fetch; what is new is that it keeps
EVERY row (eligible or not) and the per-warehouse stock behind the aggregate.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from bims_shopify.adapters.bims.client import BIMSAPIError, BIMSClient
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter

pytestmark = pytest.mark.usefixtures("sequential_paging")

INDEX = "https://bims.example.com/api/products/index.json"
STOCK = "https://bims.example.com/api/products_stocks/stock_fenicio.json"


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
def adapter(tenant):
    tenant.bims_warehouse_id = 25
    http_client = httpx.AsyncClient(
        base_url=tenant.bims_base_url, headers={"Authorization": tenant.bims_auth_header}
    )
    return BIMSERPAdapter(BIMSClient(tenant, http_client=http_client))


def _catalog(data, count=None):
    return [
        httpx.Response(200, json={"status": "ok", "count": str(count or len(data)), "data": data}),
        httpx.Response(200, json={"status": "ok", "count": str(count or len(data)), "data": []}),
    ]


def _stock_by_request(per_warehouse: dict[tuple[int, ...], dict[str, float]], requests: list):
    """Answer stock_fenicio according to the warehouse_ids in the request."""

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        key = tuple(body.get("warehouse_ids") or ())
        rows = [
            {"sku": sku, "stock": qty}
            for sku, qty in per_warehouse.get(key, {}).items()
            if sku in body["skus"]
        ]
        return httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": rows}})

    return respond


@respx.mock
async def test_keeps_every_row_but_only_asks_stock_for_eligible_skus(adapter, tenant):
    data = [
        _product(id=1, code2="OK", sell_price="1500"),
        _product(id=2, code2="OFF", enabled=False),
        _product(id=3, code2="HIDDEN", exclude_ecommerce=True),
        _product(id=4, code2=None),
    ]
    respx.get(INDEX).mock(side_effect=_catalog(data))
    stock_requests: list[dict] = []
    respx.post(STOCK).mock(side_effect=_stock_by_request({(25,): {"OK": 4.0}}, stock_requests))

    pull = await adapter.pull_catalog(tenant)

    assert [r.bims_id for r in pull.rows] == ["1", "2", "3", "4"]
    ok = pull.rows[0]
    assert (ok.sku, ok.code2, ok.sell_price, ok.company_id) == ("OK", "OK", 1500.0, 6)
    assert [r.eligible for r in pull.rows] == [True, False, False, False]
    assert pull.rows[1].enabled is False and pull.rows[2].exclude_ecommerce is True
    assert pull.stock == {"OK": 4.0}
    assert all(request["skus"] == ["OK"] for request in stock_requests)


@respx.mock
async def test_carries_pager_completeness_and_phase_timings(adapter, tenant):
    respx.get(INDEX).mock(side_effect=_catalog([_product()], count=3))  # server says 3, gives 1
    respx.post(STOCK).mock(side_effect=_stock_by_request({(25,): {"SKU-1": 2.0}}, []))

    pull = await adapter.pull_catalog(tenant)

    assert pull.reported_count == 3
    assert pull.complete is False
    assert pull.pages == 2 and pull.stop_reason == "empty_page"
    assert any("truncated pull" in w for w in pull.warnings)
    assert pull.timings["catalog_pull_seconds"] >= 0
    assert pull.timings["stock_fetch_seconds"] >= 0


@respx.mock
async def test_single_warehouse_asks_the_warehouse_and_the_unfiltered_total(adapter, tenant):
    respx.get(INDEX).mock(side_effect=_catalog([_product(code2="A"), _product(id=2, code2="B")]))
    requests: list[dict] = []
    respx.post(STOCK).mock(
        side_effect=_stock_by_request({(25,): {"A": 3.0}, (): {"A": 3.0}}, requests)
    )

    pull = await adapter.pull_catalog(tenant)

    assert sorted(tuple(r.get("warehouse_ids") or ()) for r in requests) == [(), (25,)]
    assert pull.stock_by_warehouse == {"A": {25: 3.0}}  # B is absent: unknown, not 0
    assert pull.stock_unfiltered == {"A": 3.0}
    assert pull.stock == {"A": 3.0}


@respx.mock
async def test_multi_warehouse_keeps_the_per_warehouse_quantities(adapter, tenant):
    tenant.bims_warehouse_ids = [10, 20, 30]
    respx.get(INDEX).mock(side_effect=_catalog([_product(code2="A"), _product(id=2, code2="B")]))
    requests: list[dict] = []
    respx.post(STOCK).mock(
        side_effect=_stock_by_request(
            {
                (): {"A": 10.0, "B": 0.0},
                (10,): {"A": 0.0, "B": 0.0},
                (20,): {"A": 10.0},
                (30,): {"A": 0.0, "B": 0.0},
            },
            requests,
        )
    )

    pull = await adapter.pull_catalog(tenant)

    # The sellable figure comes from the per-warehouse detail (sum of the
    # positive warehouses) and the unfiltered total, which is kept.
    assert pull.stock == {"A": 10.0, "B": 0.0}
    assert pull.stock_unfiltered == {"A": 10.0, "B": 0.0}
    assert pull.stock_by_warehouse["A"] == {10: 0.0, 20: 10.0, 30: 0.0}
    assert pull.stock_by_warehouse["B"] == {10: 0.0, 30: 0.0}  # absent in 20: not invented
    scopes = sorted(tuple(r.get("warehouse_ids") or ()) for r in requests)
    assert scopes == [(), (10,), (20,), (30,)]  # () is the unfiltered request
    request_ids = [r["_idSolicitud"] for r in requests]
    assert len(set(request_ids)) == len(request_ids)  # unique per request
    assert pull.timings["warehouse_stock_seconds"] >= 0


@respx.mock
async def test_negative_aggregate_is_still_clamped_to_zero(adapter, tenant):
    respx.get(INDEX).mock(side_effect=_catalog([_product(code2="A")]))
    respx.post(STOCK).mock(side_effect=_stock_by_request({(25,): {"A": -3.0}}, []))

    pull = await adapter.pull_catalog(tenant)

    assert pull.stock == {"A": 0.0}


@respx.mock
async def test_a_failing_warehouse_request_fails_the_pull_with_no_partial_result(adapter, tenant):
    tenant.bims_warehouse_ids = [10, 20]
    respx.get(INDEX).mock(side_effect=_catalog([_product(code2="A")]))

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("warehouse_ids") == [20]:
            return httpx.Response(200, json={"status": "ERROR", "mensaje": "warehouse 20 down"})
        return httpx.Response(
            200, json={"status": "OK", "data": {"stockPorSku": [{"sku": "A", "stock": 1}]}}
        )

    respx.post(STOCK).mock(side_effect=respond)

    with pytest.raises(BIMSAPIError, match="warehouse 20 down"):
        await adapter.pull_catalog(tenant)


@respx.mock
async def test_list_products_is_built_on_the_same_pull(adapter, tenant):
    """The live path and the snapshot path share one eligibility/absent-stock rule."""
    respx.get(INDEX).mock(
        side_effect=_catalog([_product(code2="A"), _product(id=2, code2="B"), _product(id=3, enabled=False)])
    )
    respx.post(STOCK).mock(side_effect=_stock_by_request({(25,): {"A": 2.0}}, []))

    products = await adapter.list_products(tenant)

    assert [(p.sku, p.stock) for p in products] == [("A", 2.0), ("B", None)]


# -- the unfiltered total: BIMS's warehouse filter can answer 0 for real stock --


def _outage_world(requests):
    """Production on 2026-11-02: every per-warehouse answer is 0, the unfiltered total is real."""
    return _stock_by_request(
        {
            (): {"SATORI-S": 6.0, "SATORI-L": 7.0},
            **{(wid,): {"SATORI-S": 0.0, "SATORI-L": 0.0} for wid in (20, 24, 25, 26, 27)},
        },
        requests,
    )


@respx.mock
async def test_regression_cyber_monday_all_zero_warehouses_with_real_total_is_sellable(
    adapter, tenant
):
    """REGRESSION: the pull must not publish 0 for the catalog while BIMS has stock."""
    tenant.bims_warehouse_ids = [20, 24, 25, 26, 27]
    respx.get(INDEX).mock(
        side_effect=_catalog([_product(code2="SATORI-S"), _product(id=2, code2="SATORI-L")])
    )
    respx.post(STOCK).mock(side_effect=_outage_world([]))

    pull = await adapter.pull_catalog(tenant)

    assert pull.stock == {"SATORI-S": 6.0, "SATORI-L": 7.0}
    assert pull.stock_unfiltered == {"SATORI-S": 6.0, "SATORI-L": 7.0}
    assert pull.stock_by_warehouse["SATORI-S"] == dict.fromkeys((20, 24, 25, 26, 27), 0.0)
    assert {d.sku for d in pull.stock_discrepancies} == {"SATORI-S", "SATORI-L"}


@respx.mock
async def test_negative_warehouse_still_does_not_cancel_a_positive_one(adapter, tenant):
    tenant.bims_warehouse_ids = [24, 27]
    respx.get(INDEX).mock(side_effect=_catalog([_product(code2="BOXER-M")]))
    respx.post(STOCK).mock(
        side_effect=_stock_by_request(
            {(): {"BOXER-M": 7.0}, (24,): {"BOXER-M": -3.0}, (27,): {"BOXER-M": 10.0}}, []
        )
    )

    pull = await adapter.pull_catalog(tenant)

    assert pull.stock == {"BOXER-M": 10.0}
    assert pull.stock_by_warehouse["BOXER-M"] == {24: -3.0, 27: 10.0}  # raw, not clamped
    assert pull.stock_discrepancies == []  # 10 + -3 == 7: BIMS agrees with itself


@respx.mock
async def test_the_unfiltered_request_never_carries_warehouse_ids(adapter, tenant):
    tenant.bims_warehouse_ids = [20, 24]
    respx.get(INDEX).mock(side_effect=_catalog([_product(code2="A")]))
    requests: list[dict] = []
    respx.post(STOCK).mock(side_effect=_stock_by_request({(): {"A": 1.0}}, requests))

    await adapter.pull_catalog(tenant)

    unfiltered = [r for r in requests if "warehouse_ids" not in r]
    assert len(unfiltered) == 1 and unfiltered[0]["skus"] == ["A"]


@respx.mock
async def test_the_live_list_products_path_also_survives_the_broken_filter(adapter, tenant):
    tenant.bims_warehouse_ids = [20, 24]
    respx.get(INDEX).mock(side_effect=_catalog([_product(code2="A")]))
    respx.post(STOCK).mock(
        side_effect=_stock_by_request({(): {"A": 6.0}, (20, 24): {"A": 0.0}}, [])
    )

    products = await adapter.list_products(tenant)

    assert [(p.sku, p.stock) for p in products] == [("A", 6.0)]


@respx.mock
async def test_detail_only_rule_keeps_the_old_figure_but_still_records_the_total(adapter, tenant):
    tenant.bims_warehouse_ids = [20, 24]
    tenant.field_mappings = {"stock_figure_rule": "detail_only"}
    respx.get(INDEX).mock(side_effect=_catalog([_product(code2="A")]))
    respx.post(STOCK).mock(
        side_effect=_stock_by_request(
            {(): {"A": 6.0}, (20,): {"A": 0.0}, (24,): {"A": 0.0}}, []
        )
    )

    pull = await adapter.pull_catalog(tenant)

    assert pull.stock == {"A": 0.0}
    assert pull.stock_unfiltered == {"A": 6.0}
    assert len(pull.stock_discrepancies) == 1
