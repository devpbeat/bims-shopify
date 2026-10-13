"""The catalog pull must report whether it is complete, in every command that uses it."""
from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.ops import catalog as catalog_ops
from bims_shopify.ops.catalog import fetch_bims_catalog_pull, fetch_bims_catalog_rows

# These mocks script BIMS responses by call order, so page sequentially.
pytestmark = pytest.mark.usefixtures("sequential_paging")


BIMS_URL = "https://bims.example.com"


def _product(i: int, **extra: Any) -> dict[str, Any]:
    return {
        "Product": {
            "id": i,
            "name": f"Shirt {i}",
            "code2": f"SKU-{i}",
            "sell_price": 1000,
            "enabled": True,
            "exclude_ecommerce": False,
            **extra,
        }
    }


def _envelope(rows: list[dict], count: int | None) -> httpx.Response:
    body: dict[str, Any] = {"status": "ok", "data": rows}
    if count is not None:
        body["count"] = count
    return httpx.Response(200, json=body)


@respx.mock
async def test_pull_survives_a_short_page_mid_catalog(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json")
    route.side_effect = [
        _envelope([_product(i) for i in (1, 2, 3)], 5),
        _envelope([_product(i) for i in (4, 5)], 5),
        _envelope([], 5),
    ]
    pull = await fetch_bims_catalog_pull(BIMSClient(tenant), tenant.bims_company_id)

    assert [r["id"] for r in pull.rows] == [1, 2, 3, 4, 5]
    assert pull.complete is True
    assert [c.request.url.params["offset"] for c in route.calls] == ["0", "3", "5"]


@respx.mock
async def test_fetch_bims_catalog_rows_still_returns_the_plain_list(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json")
    route.side_effect = [_envelope([_product(1)], 1), _envelope([], 1)]
    rows = await fetch_bims_catalog_rows(BIMSClient(tenant), tenant.bims_company_id)
    assert [r["id"] for r in rows] == [1]


@respx.mock
async def test_pull_reports_truncation_against_the_server_count(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json")
    route.side_effect = [_envelope([_product(1), _product(2)], 9), _envelope([], 9)]
    pull = await fetch_bims_catalog_pull(BIMSClient(tenant), tenant.bims_company_id)
    assert pull.summary()["complete"] is False
    assert pull.summary()["reported_count"] == 9
    assert pull.summary()["rows"] == 2


# -- fakes shared by the command-level tests ----------------------------------


def _install(monkeypatch, pages: list[dict[str, Any]]):
    """BIMS fake serving ``pages`` (envelopes) in order; Shopify fake with an empty shop."""

    class _FakeBims:
        def __init__(self, tenant, **kwargs):
            self.calls = 0

        async def get(self, path, params=None):
            index = _FakeBims.served
            _FakeBims.served += 1
            return pages[index] if index < len(pages) else {"data": []}

        async def stock_fenicio(self, *, skus, warehouse_ids, request_id):
            return {"data": {"stockPorSku": []}}

        async def aclose(self):
            pass

    _FakeBims.served = 0

    class _FakeShopify:
        def __init__(self, tenant):
            pass

        async def iter_all_products_with_skus(self):
            return
            yield  # pragma: no cover

        async def iter_all_products(self):
            return
            yield  # pragma: no cover

        async def iter_all_variants(self):
            return
            yield  # pragma: no cover

        async def fetch_publications(self):
            return [{"id": "gid://shopify/Publication/1", "name": "Online Store"}]

        async def aclose(self):
            pass

    monkeypatch.setattr(catalog_ops, "BIMSClient", _FakeBims)
    monkeypatch.setattr(catalog_ops, "ShopifyClient", _FakeShopify)


async def test_import_reports_catalog_pull(tenant, monkeypatch):
    _install(
        monkeypatch,
        [{"count": 4, "data": [_product(1), _product(2)]}, {"count": 4, "data": []}],
    )
    args = catalog_ops._build_arg_parser().parse_args(["acme", "import"])
    summary = await catalog_ops._run_import(tenant, args)

    pull = summary["catalog_pull"]
    assert pull["rows"] == 2
    assert pull["reported_count"] == 4
    assert pull["complete"] is False


async def test_import_reports_unknown_completeness_without_a_server_count(tenant, monkeypatch):
    _install(monkeypatch, [{"data": [_product(1)]}, {"data": []}])
    args = catalog_ops._build_arg_parser().parse_args(["acme", "import"])
    summary = await catalog_ops._run_import(tenant, args)
    assert summary["catalog_pull"]["complete"] is None
    assert summary["catalog_pull"]["reported_count"] is None


async def test_status_reports_catalog_pull(tenant, monkeypatch):
    _install(monkeypatch, [{"count": 3, "data": [_product(1)]}, {"count": 3, "data": []}])

    async def _no_activity(tenant_id):
        return {}

    async def _no_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(catalog_ops, "_fetch_last_activity", _no_activity)
    monkeypatch.setattr(catalog_ops, "_audit", _no_audit)
    args = catalog_ops._build_arg_parser().parse_args(["acme", "status"])
    report = await catalog_ops._run_status(tenant, args)

    assert report["catalog_pull"]["complete"] is False
    assert report["catalog_pull"]["rows"] == 1
    assert report["catalog_pull"]["reported_count"] == 3
    assert report["catalog_pull"]["duration_seconds"] >= 0
    assert report["timings"]["catalog_pull_seconds"] == report["catalog_pull"]["duration_seconds"]
    assert report["timings"]["stock_fetch_seconds"] >= 0


async def test_diagnose_reports_catalog_pull_and_warns_when_a_miss_is_not_conclusive(
    tenant, monkeypatch
):
    # Both phases see a truncated pull and the query matches nothing.
    _install(
        monkeypatch,
        [
            {"count": 50, "data": [_product(1)]},
            {"count": 50, "data": []},
            {"count": 50, "data": [_product(1)]},
            {"count": 50, "data": []},
        ],
    )
    report = await catalog_ops.run_diagnose(tenant, {"query": "not-there"})

    assert report["total_matches"] == 0
    assert report["searched_all_companies"] is True
    assert report["catalog_pull"]["complete"] is False
    assert any("incomplete" in note and "NOT" in note for note in report["notes"])


async def test_diagnose_complete_pull_adds_no_incompleteness_note(tenant, monkeypatch):
    _install(monkeypatch, [{"count": 1, "data": [_product(1)]}, {"count": 1, "data": []}])
    report = await catalog_ops.run_diagnose(tenant, {"query": "shirt 1"})
    assert report["catalog_pull"]["complete"] is True
    assert report["notes"] == []
