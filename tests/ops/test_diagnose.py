"""Tests for the read-only ``diagnose`` ops command and its shared pipeline helpers."""
from __future__ import annotations

import httpx
import respx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.ops.catalog import (
    bims_gate_failure,
    fetch_bims_catalog_rows,
    filter_and_dedupe_rows,
    has_stock,
)

BIMS_URL = "https://bims.example.com"


# -- shared pipeline helpers -------------------------------------------------


def test_bims_gate_failure_passes_clean_row():
    raw = {"id": 1, "enabled": True, "exclude_ecommerce": False, "code2": "SKU-1"}
    assert bims_gate_failure(raw) is None


def test_bims_gate_failure_names_each_gate():
    assert bims_gate_failure({"enabled": False, "code2": "A"}) == "disabled"
    assert bims_gate_failure({"enabled": "0", "code2": "A"}) == "disabled"
    assert (
        bims_gate_failure({"enabled": True, "exclude_ecommerce": "1", "code2": "A"})
        == "excluded_from_ecommerce"
    )
    assert bims_gate_failure({"enabled": True, "code2": ""}) == "missing_code2"
    assert bims_gate_failure({"enabled": True, "code2": None}) == "missing_code2"
    assert bims_gate_failure({"enabled": True}) == "missing_code2"


def test_bims_gate_failure_defaults_match_the_pipeline():
    """Absent ``enabled`` means enabled; absent ``exclude_ecommerce`` means included."""
    assert bims_gate_failure({"code2": "A"}) is None


def test_bims_gate_failure_precedence_disabled_before_missing_code2():
    assert bims_gate_failure({"enabled": False, "code2": ""}) == "disabled"
    assert (
        bims_gate_failure({"enabled": True, "exclude_ecommerce": True, "code2": ""})
        == "excluded_from_ecommerce"
    )


def test_filter_and_dedupe_rows_agrees_with_bims_gate_failure():
    raws = [
        {"id": 1, "name": "ok", "code2": "A", "enabled": True},
        {"id": 2, "name": "off", "code2": "B", "enabled": False},
        {"id": 3, "name": "ex", "code2": "C", "enabled": True, "exclude_ecommerce": True},
        {"id": 4, "name": "nocode", "code2": "", "enabled": True},
    ]
    rows, _ = filter_and_dedupe_rows(raws)
    assert [r.bims_id for r in rows] == [
        str(r["id"]) for r in raws if bims_gate_failure(r) is None
    ]


def test_has_stock_is_strictly_greater_than_zero():
    assert has_stock({"A": 1.0}, "A") is True
    assert has_stock({"A": 0.0}, "A") is False
    assert has_stock({"A": -2.0}, "A") is False
    assert has_stock({}, "A") is False


# -- fetch_bims_catalog_rows mode --------------------------------------------


@respx.mock
async def test_fetch_bims_catalog_rows_defaults_to_simple_mode(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )
    await fetch_bims_catalog_rows(BIMSClient(tenant), tenant.bims_company_id)
    assert route.calls[0].request.url.params["mode"] == "simple"


@respx.mock
async def test_fetch_bims_catalog_rows_accepts_explicit_mode(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "data": [{"Product": {"id": 1, "code": "TAG-1"}}]}
        )
    )
    rows = await fetch_bims_catalog_rows(BIMSClient(tenant), tenant.bims_company_id, mode="full")
    assert route.calls[0].request.url.params["mode"] == "full"
    assert rows == [{"id": 1, "code": "TAG-1"}]


# -- row matching ------------------------------------------------------------


def _raw(
    bims_id: int | str,
    name: str = "Blue Shirt (M)",
    *,
    code: str | None = None,
    code2: str | None = "SKU-1",
    enabled: object = True,
    exclude: object = False,
    price: float = 1000,
) -> dict:
    return {
        "id": bims_id,
        "code": code,
        "code2": code2,
        "name": name,
        "enabled": enabled,
        "exclude_ecommerce": exclude,
        "sell_price": price,
    }


def test_match_catalog_rows_by_id():
    from bims_shopify.ops.catalog import match_catalog_rows

    rows = [_raw(101, "A", code2="X1"), _raw(1010, "B", code2="X2")]
    assert [r["id"] for r in match_catalog_rows(rows, "101")] == [101]


def test_match_catalog_rows_by_code_case_insensitive():
    from bims_shopify.ops.catalog import match_catalog_rows

    rows = [_raw(1, "A", code="Tag-77", code2="X1"), _raw(2, "B", code=None, code2="X2")]
    assert [r["id"] for r in match_catalog_rows(rows, "tag-77")] == [1]


def test_match_catalog_rows_by_code2_case_insensitive():
    from bims_shopify.ops.catalog import match_catalog_rows

    rows = [_raw(1, "A", code2="ab-123"), _raw(2, "B", code2="ZZ")]
    assert [r["id"] for r in match_catalog_rows(rows, " AB-123 ")] == [1]


def test_match_catalog_rows_by_name_substring_returns_every_match():
    from bims_shopify.ops.catalog import match_catalog_rows

    rows = [
        _raw(1, "Blue Shirt (S)", code2="A"),
        _raw(2, "BLUE SHIRT (M)", code2="B"),
        _raw(3, "Red Pants", code2="C"),
    ]
    assert [r["id"] for r in match_catalog_rows(rows, "blue shirt")] == [1, 2]


def test_match_catalog_rows_ids_codes_are_exact_not_substring():
    from bims_shopify.ops.catalog import match_catalog_rows

    rows = [_raw(1, "A", code="TAG-77", code2="SKU-77")]
    assert match_catalog_rows(rows, "77") == []


def test_match_catalog_rows_blank_query_matches_nothing():
    from bims_shopify.ops.catalog import match_catalog_rows

    assert match_catalog_rows([_raw(1)], "   ") == []


# -- verdicts (pure) ---------------------------------------------------------

ONLINE_STORE = "gid://shopify/Publication/1"


def _shopify(
    sku: str = "SKU-1",
    *,
    status: str = "ACTIVE",
    publications: list[str] | None = None,
    media: int = 2,
    product_id: str = "gid://shopify/Product/1",
) -> dict:
    return {
        sku: {
            "id": product_id,
            "title": "Blue Shirt",
            "status": status,
            "skus": [sku],
            "media_count": media,
            "published_publication_ids": [ONLINE_STORE] if publications is None else publications,
        }
    }


def _diagnose(raws, matched_ids, *, stock=None, per_warehouse=None, total=None,
              shopify=None, publication=ONLINE_STORE):
    from bims_shopify.ops.catalog import compute_diagnosis

    matched = [r for r in raws if r["id"] in matched_ids]
    return compute_diagnosis(
        raws,
        matched,
        stock_by_sku=stock if stock is not None else {},
        stock_by_warehouse=per_warehouse if per_warehouse is not None else {},
        stock_total_all=total if total is not None else {},
        shopify_index=shopify if shopify is not None else {},
        online_store_publication_id=publication,
    )


def test_verdict_disabled():
    (d,) = _diagnose([_raw(1, enabled=False)], {1})
    assert d.verdict == "disabled"
    assert d.shopify is None


def test_verdict_excluded_from_ecommerce():
    (d,) = _diagnose([_raw(1, exclude="1")], {1})
    assert d.verdict == "excluded_from_ecommerce"


def test_verdict_missing_code2():
    (d,) = _diagnose([_raw(1, code2=None)], {1})
    assert d.verdict == "missing_code2"
    assert d.stock_by_warehouse is None
    assert d.stock_total_all_warehouses is None


def test_precedence_disabled_before_missing_code2():
    (d,) = _diagnose([_raw(1, enabled=False, code2="")], {1})
    assert d.verdict == "disabled"


def test_precedence_disabled_before_excluded():
    (d,) = _diagnose([_raw(1, enabled=False, exclude=True)], {1})
    assert d.verdict == "disabled"


def test_verdict_duplicate_code2_names_the_winning_row():
    raws = [_raw(1, "Winner (M)", code2="DUP"), _raw(2, "Loser (M)", code2="DUP")]
    winner, loser = _diagnose(raws, {1, 2}, stock={"DUP": 3.0})
    assert loser.verdict == "duplicate_code2"
    assert loser.duplicate_of == {"bims_id": "1", "name": "Winner (M)"}
    assert winner.verdict == "not_in_shopify"
    assert winner.duplicate_of is None


def test_disabled_row_does_not_win_a_duplicate_code2():
    """The pipeline skips disabled rows before dedupe, so the next row wins."""
    raws = [_raw(1, "Off", code2="DUP", enabled=False), _raw(2, "On", code2="DUP")]
    off, on = _diagnose(raws, {1, 2}, stock={"DUP": 1.0})
    assert off.verdict == "disabled"
    assert on.verdict != "duplicate_code2"


def test_verdict_shadowed_variant_when_option_value_collides():
    raws = [
        _raw(1, "Blue Shirt (M)", code2="A-M"),
        _raw(2, "Blue Shirt (M)", code2="B-M"),
    ]
    kept, shadowed = _diagnose(raws, {1, 2}, stock={"A-M": 2.0, "B-M": 2.0})
    assert kept.verdict == "not_in_shopify"
    assert shadowed.verdict == "shadowed_variant"
    assert shadowed.shadowed_by == {"kept_sku": "A-M", "title": "Blue Shirt", "size": "M"}


def test_verdict_no_stock():
    (d,) = _diagnose([_raw(1)], {1}, stock={"SKU-1": 0.0})
    assert d.verdict == "no_stock"


def test_verdict_no_stock_when_sku_absent_from_stock_response():
    (d,) = _diagnose([_raw(1)], {1}, stock={})
    assert d.verdict == "no_stock"


def test_no_stock_requires_strictly_positive_stock():
    (d,) = _diagnose([_raw(1)], {1}, stock={"SKU-1": -1.0})
    assert d.verdict == "no_stock"


def test_stock_gate_is_group_level_like_only_with_stock():
    """A zero-stock size is still imported when a sibling size has stock."""
    raws = [
        _raw(1, "Blue Shirt (S)", code2="S"),
        _raw(2, "Blue Shirt (M)", code2="M"),
    ]
    small, medium = _diagnose(raws, {1, 2}, stock={"S": 0.0, "M": 4.0})
    assert small.verdict == "not_in_shopify"
    assert small.group_has_stock is True
    assert medium.verdict == "not_in_shopify"


def test_no_stock_when_whole_group_is_empty():
    raws = [_raw(1, "Blue Shirt (S)", code2="S"), _raw(2, "Blue Shirt (M)", code2="M")]
    small, medium = _diagnose(raws, {1, 2}, stock={"S": 0.0, "M": 0.0})
    assert small.verdict == medium.verdict == "no_stock"
    assert small.group_has_stock is False


def test_no_stock_in_synced_warehouses_but_stock_elsewhere():
    """Stock only in a warehouse the tenant does not sync -> no_stock, total > 0."""
    (d,) = _diagnose(
        [_raw(1)],
        {1},
        stock={"SKU-1": 0.0},
        per_warehouse={5: {"SKU-1": 0.0}, 6: {"SKU-1": 0.0}},
        total={"SKU-1": 7.0},
    )
    assert d.verdict == "no_stock"
    assert d.stock_by_warehouse == {5: 0.0, 6: 0.0}
    assert d.stock_total_all_warehouses == 7.0


def test_stock_by_warehouse_reports_each_synced_warehouse():
    (d,) = _diagnose(
        [_raw(1)],
        {1},
        stock={"SKU-1": 5.0},
        per_warehouse={5: {"SKU-1": 3.0}, 6: {"SKU-1": 2.0}},
        total={"SKU-1": 9.0},
    )
    assert d.stock_by_warehouse == {5: 3.0, 6: 2.0}
    assert d.stock_total_all_warehouses == 9.0


def test_verdict_not_in_shopify_is_the_alarming_one():
    (d,) = _diagnose([_raw(1)], {1}, stock={"SKU-1": 3.0})
    assert d.verdict == "not_in_shopify"
    assert d.shopify is None


def test_verdict_draft():
    (d,) = _diagnose([_raw(1)], {1}, stock={"SKU-1": 3.0}, shopify=_shopify(status="DRAFT"))
    assert d.verdict == "draft"
    assert d.shopify["status"] == "DRAFT"


def test_verdict_archived():
    (d,) = _diagnose([_raw(1)], {1}, stock={"SKU-1": 3.0}, shopify=_shopify(status="ARCHIVED"))
    assert d.verdict == "archived"


def test_verdict_not_published():
    (d,) = _diagnose(
        [_raw(1)], {1}, stock={"SKU-1": 3.0}, shopify=_shopify(publications=["gid://other"])
    )
    assert d.verdict == "not_published"
    assert d.shopify["on_online_store"] is False


def test_verdict_live():
    (d,) = _diagnose([_raw(1)], {1}, stock={"SKU-1": 3.0}, shopify=_shopify())
    assert d.verdict == "live"
    assert d.shopify == {
        "product_id": "gid://shopify/Product/1",
        "title": "Blue Shirt",
        "status": "ACTIVE",
        "media_count": 2,
        "published_publication_ids": [ONLINE_STORE],
        "on_online_store": True,
    }


def test_shopify_side_does_not_depend_on_bims_stock_for_live_rows():
    """A live product with zero stock is still reported with its Shopify facts."""
    (d,) = _diagnose([_raw(1)], {1}, stock={"SKU-1": 0.0}, shopify=_shopify())
    assert d.verdict == "no_stock"
    assert d.shopify is not None
    assert d.shopify["status"] == "ACTIVE"


def test_unresolved_online_store_publication_degrades_gracefully():
    (d,) = _diagnose(
        [_raw(1)], {1}, stock={"SKU-1": 3.0}, shopify=_shopify(), publication=None
    )
    assert d.verdict == "publication_unknown"
    assert d.shopify["on_online_store"] is None
    assert d.shopify["status"] == "ACTIVE"


def test_unresolved_publication_does_not_affect_draft_verdict():
    (d,) = _diagnose(
        [_raw(1)], {1}, stock={"SKU-1": 3.0}, shopify=_shopify(status="DRAFT"), publication=None
    )
    assert d.verdict == "draft"


def test_bims_fields_are_reported_as_plain_data():
    raws = [_raw(1, "Blue Shirt (M)", code="TAG-1", code2="SKU-1", price=1500)]
    (d,) = _diagnose(raws, {1}, stock={"SKU-1": 1.0})
    assert d.bims == {
        "id": 1,
        "code": "TAG-1",
        "code2": "SKU-1",
        "name": "Blue Shirt (M)",
        "company_id": None,
        "enabled": True,
        "exclude_ecommerce": False,
        "sell_price": 1500,
    }


def test_diagnosis_stock_skus_include_own_code2_and_group_siblings():
    from bims_shopify.ops.catalog import diagnosis_stock_skus

    raws = [
        _raw(1, "Blue Shirt (S)", code2="S"),
        _raw(2, "Blue Shirt (M)", code2="M"),
        _raw(3, "Red Pants", code2="P"),
        _raw(4, "Off Hat", code2="H", enabled=False),
    ]
    matched = [raws[0], raws[3]]
    assert sorted(diagnosis_stock_skus(raws, matched)) == ["H", "M", "S"]


# -- report ------------------------------------------------------------------


def test_build_diagnose_report_shape_and_verdict_counts():
    from bims_shopify.ops.catalog import DiagnoseReport, build_diagnose_report

    raws = [_raw(1, enabled=False), _raw(2, "Other", code2="O")]
    diagnoses = _diagnose(raws, {1, 2}, stock={"O": 1.0})
    report = build_diagnose_report(
        DiagnoseReport(
            query="shirt",
            total_matches=2,
            diagnoses=diagnoses,
            online_store_publication_id=ONLINE_STORE,
            notes=["a note"],
        )
    )
    assert report["query"] == "shirt"
    assert report["total_matches"] == 2
    assert report["diagnosed"] == 2
    assert report["truncated"] is False
    assert report["online_store_publication_id"] == ONLINE_STORE
    assert report["notes"] == ["a note"]
    assert report["verdict_counts"] == {"disabled": 1, "not_in_shopify": 1}
    assert report["rows"][0]["verdict"] == "disabled"
    assert report["rows"][0]["bims"]["id"] == 1
    assert "stock_by_warehouse" in report["rows"][0]
    assert "stock_total_all_warehouses" in report["rows"][0]


def test_build_diagnose_report_flags_truncation():
    from bims_shopify.ops.catalog import DiagnoseReport, build_diagnose_report

    report = build_diagnose_report(
        DiagnoseReport(query="x", total_matches=40, diagnoses=[], online_store_publication_id=None)
    )
    assert report["truncated"] is True
    assert report["total_matches"] == 40
    assert report["diagnosed"] == 0


def test_diagnose_max_matches_constant():
    from bims_shopify.ops.catalog import DIAGNOSE_MAX_MATCHES

    assert DIAGNOSE_MAX_MATCHES == 25


def test_shopify_side_reported_even_for_a_bims_gate_failure():
    """A row disabled in BIMS but still present in Shopify shows both facts."""
    (d,) = _diagnose([_raw(1, enabled=False)], {1}, shopify=_shopify())
    assert d.verdict == "disabled"
    assert d.shopify is not None
    assert d.shopify["status"] == "ACTIVE"


# -- runner (_run_diagnose / run_diagnose) -----------------------------------


class _Recorder:
    """Shared call log for the fake clients; also proves the command is read-only."""

    def __init__(self) -> None:
        self.catalog_params: list[dict] = []
        self.stock_calls: list[dict] = []
        self.progress: list[str] = []


def _install_fakes(
    monkeypatch,
    *,
    raw_rows: list[dict],
    stock_by_wh: dict[int | None, dict[str, float]],
    products: list[dict],
    publications: list[dict] | None = None,
    publications_error: Exception | None = None,
) -> _Recorder:
    from bims_shopify.ops import catalog as catalog_ops

    rec = _Recorder()
    pubs = (
        publications
        if publications is not None
        else [{"id": ONLINE_STORE, "name": "Online Store"}]
    )

    class _FakeBims:
        def __init__(self, tenant, **kwargs):
            pass

        async def get(self, path, params=None):
            rec.catalog_params.append(dict(params or {}))
            offset = (params or {}).get("offset", 0)
            company = (params or {}).get("company_id")
            visible = [
                r
                for r in raw_rows
                if company is None
                or r.get("company_id") is None
                or str(r["company_id"]) == str(company)
            ]
            page = visible[offset : offset + catalog_ops.BIMS_PAGE_LIMIT]
            return {"data": [{"Product": r} for r in page]}

        async def stock_fenicio(self, *, skus, warehouse_ids, request_id):
            rec.stock_calls.append({"skus": list(skus), "warehouse_ids": warehouse_ids})
            if warehouse_ids is None:
                scope: dict[str, float] = {}
                for per_sku in stock_by_wh.values():
                    for sku, qty in per_sku.items():
                        scope[sku] = scope.get(sku, 0.0) + qty
            else:
                scope = {}
                for wid in warehouse_ids:
                    for sku, qty in stock_by_wh.get(wid, {}).items():
                        scope[sku] = scope.get(sku, 0.0) + qty
            return {
                "data": {
                    "stockPorSku": [
                        {"sku": sku, "stock": scope[sku]} for sku in skus if sku in scope
                    ]
                }
            }

        async def aclose(self):
            pass

    class _FakeShopify:
        """Read-only surface only: any mutation attempt raises AttributeError."""

        def __init__(self, tenant):
            pass

        async def iter_all_products_with_skus(self):
            for product in products:
                yield product

        async def fetch_publications(self):
            if publications_error is not None:
                raise publications_error
            return pubs

        async def aclose(self):
            pass

    monkeypatch.setattr(catalog_ops, "BIMSClient", _FakeBims)
    monkeypatch.setattr(catalog_ops, "ShopifyClient", _FakeShopify)
    return rec


def _sf_product(sku="SKU-1", *, status="ACTIVE", publications=None):
    return {
        "id": "gid://shopify/Product/1",
        "title": "Blue Shirt",
        "status": status,
        "skus": [sku],
        "media_count": 1,
        "published_publication_ids": [ONLINE_STORE] if publications is None else publications,
    }


async def test_run_diagnose_end_to_end_reports_live_row(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.bims_warehouse_id = 5
    rec = _install_fakes(
        monkeypatch,
        raw_rows=[_raw(1, "Blue Shirt (M)", code="TAG-9", code2="SKU-1")],
        stock_by_wh={5: {"SKU-1": 3.0}, 9: {"SKU-1": 4.0}},
        products=[_sf_product()],
    )
    messages: list[str] = []
    report = await catalog_ops.run_diagnose(
        tenant, {"query": "tag-9"}, progress=lambda d, t, m: messages.append(m)
    )

    assert report["total_matches"] == 1
    assert report["truncated"] is False
    (row,) = report["rows"]
    assert row["verdict"] == "live"
    assert row["bims"]["code"] == "TAG-9"
    assert row["stock_by_warehouse"] == {5: 3.0}
    assert row["stock_total_all_warehouses"] == 7.0
    assert row["shopify"]["on_online_store"] is True
    assert report["online_store_publication_id"] == ONLINE_STORE
    assert messages == [
        "pulling BIMS catalog",
        "checking stock",
        "scanning Shopify",
    ]
    # Full catalog, no incremental filter, cheap "simple" mode.
    assert all(p["mode"] == "simple" for p in rec.catalog_params)
    assert all("last_update" not in p for p in rec.catalog_params)


async def test_run_diagnose_calls_stock_once_per_synced_warehouse_plus_unscoped(
    tenant, monkeypatch
):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.bims_warehouse_ids = [5, 6]
    rec = _install_fakes(
        monkeypatch,
        raw_rows=[_raw(1, code2="SKU-1")],
        stock_by_wh={},
        products=[],
    )
    await catalog_ops.run_diagnose(tenant, {"query": "SKU-1"})
    scopes = [c["warehouse_ids"] for c in rec.stock_calls]
    assert sorted(scopes, key=lambda s: (s is None, s or [])) == [[5], [5, 6], [6], None]


async def test_run_diagnose_unsynced_warehouse_stock_is_visible(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.bims_warehouse_id = 5
    _install_fakes(
        monkeypatch,
        raw_rows=[_raw(1, code2="SKU-1")],
        stock_by_wh={77: {"SKU-1": 8.0}},
        products=[],
    )
    report = await catalog_ops.run_diagnose(tenant, {"query": "1"})
    (row,) = report["rows"]
    assert row["verdict"] == "no_stock"
    assert row["stock_by_warehouse"] == {5: 0.0}
    assert row["stock_total_all_warehouses"] == 8.0


async def test_run_diagnose_truncates_past_the_cap(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    raws = [_raw(i, f"Shirt {i}", code2=f"S{i}") for i in range(1, 31)]
    _install_fakes(monkeypatch, raw_rows=raws, stock_by_wh={}, products=[])
    report = await catalog_ops.run_diagnose(tenant, {"query": "shirt"})

    assert report["total_matches"] == 30
    assert report["diagnosed"] == catalog_ops.DIAGNOSE_MAX_MATCHES == 25
    assert report["truncated"] is True
    assert [r["bims"]["id"] for r in report["rows"]] == list(range(1, 26))


async def test_run_diagnose_no_match_returns_empty_report(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    rec = _install_fakes(
        monkeypatch, raw_rows=[_raw(1)], stock_by_wh={}, products=[]
    )
    report = await catalog_ops.run_diagnose(tenant, {"query": "nothing-like-this"})
    assert report["total_matches"] == 0
    assert report["rows"] == []
    assert rec.stock_calls == []


async def test_run_diagnose_degrades_when_online_store_publication_missing(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    _install_fakes(
        monkeypatch,
        raw_rows=[_raw(1, code2="SKU-1")],
        stock_by_wh={1: {"SKU-1": 3.0}},
        products=[_sf_product()],
        publications=[{"id": "gid://shopify/Publication/9", "name": "Point of Sale"}],
    )
    report = await catalog_ops.run_diagnose(tenant, {"query": "SKU-1"})

    assert report["online_store_publication_id"] is None
    (row,) = report["rows"]
    assert row["verdict"] == "publication_unknown"
    assert row["shopify"]["on_online_store"] is None
    assert any("Online Store" in note for note in report["notes"])


async def test_run_diagnose_degrades_when_publication_lookup_errors(tenant, monkeypatch):
    from bims_shopify.adapters.shopify.client import ShopifyGraphQLError
    from bims_shopify.ops import catalog as catalog_ops

    _install_fakes(
        monkeypatch,
        raw_rows=[_raw(1, code2="SKU-1")],
        stock_by_wh={1: {"SKU-1": 3.0}},
        products=[_sf_product()],
        publications_error=ShopifyGraphQLError("boom"),
    )
    report = await catalog_ops.run_diagnose(tenant, {"query": "SKU-1"})
    assert report["online_store_publication_id"] is None
    assert report["rows"][0]["verdict"] == "publication_unknown"
    assert report["notes"]


async def test_run_diagnose_is_read_only_and_never_audits(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    _install_fakes(
        monkeypatch,
        raw_rows=[_raw(1, code2="SKU-1")],
        stock_by_wh={1: {"SKU-1": 3.0}},
        products=[_sf_product(status="DRAFT")],
    )

    async def _no_audit(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("diagnose must not write audit rows")

    monkeypatch.setattr(catalog_ops, "_audit", _no_audit)
    report = await catalog_ops.run_diagnose(tenant, {"query": "SKU-1"})
    assert report["rows"][0]["verdict"] == "draft"
    assert "dry_run" not in report and "apply" not in report


def test_argparse_diagnose_subcommand_takes_query_and_no_apply():
    from bims_shopify.ops import catalog as catalog_ops

    parser = catalog_ops._build_arg_parser()
    args = parser.parse_args(["acme", "diagnose", "blue shirt"])
    assert args.command == "diagnose"
    assert args.query == "blue shirt"
    assert not hasattr(args, "apply")


def test_job_runner_registers_diagnose():
    from bims_shopify.ops import catalog as catalog_ops
    from bims_shopify.ops.job_runner import _COMMAND_RUNNERS

    assert _COMMAND_RUNNERS["diagnose"] is catalog_ops.run_diagnose


# -- wrong_company (rows BIMS only returns when the company filter is dropped) -


def _diagnose_in_company(raws, matched_ids, *, tenant_company_id=1, stock=None, shopify=None):
    from bims_shopify.ops.catalog import compute_diagnosis

    matched = [r for r in raws if r["id"] in matched_ids]
    return compute_diagnosis(
        raws,
        matched,
        stock_by_sku=stock if stock is not None else {},
        stock_by_warehouse={},
        stock_total_all={},
        shopify_index=shopify if shopify is not None else {},
        online_store_publication_id=ONLINE_STORE,
        tenant_company_id=tenant_company_id,
    )


def _company_raw(bims_id, company_id, **kwargs):
    return {**_raw(bims_id, **kwargs), "company_id": company_id}


@respx.mock
async def test_fetch_bims_catalog_rows_omits_company_id_when_none(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )
    await fetch_bims_catalog_rows(BIMSClient(tenant), None, mode="full")
    assert "company_id" not in route.calls[0].request.url.params


@respx.mock
async def test_fetch_bims_catalog_rows_still_sends_company_id_by_default(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )
    await fetch_bims_catalog_rows(BIMSClient(tenant), tenant.bims_company_id)
    assert route.calls[0].request.url.params["company_id"] == str(tenant.bims_company_id)


def test_verdict_wrong_company_reports_both_company_ids():
    (d,) = _diagnose_in_company([_company_raw(1, 2)], {1}, tenant_company_id=6)
    assert d.verdict == "wrong_company"
    assert d.wrong_company == {"row_company_id": 2, "tenant_company_id": 6}
    assert d.bims["company_id"] == 2


def test_wrong_company_takes_precedence_over_disabled():
    (d,) = _diagnose_in_company([_company_raw(1, 2, enabled=False)], {1}, tenant_company_id=6)
    assert d.verdict == "wrong_company"


def test_wrong_company_takes_precedence_over_missing_code2():
    (d,) = _diagnose_in_company([_company_raw(1, 2, code2=None)], {1}, tenant_company_id=6)
    assert d.verdict == "wrong_company"


def test_wrong_company_compares_ids_as_strings():
    """BIMS may return ids as strings; "6" is the tenant's company 6."""
    (d,) = _diagnose_in_company(
        [_company_raw(1, "6")], {1}, tenant_company_id=6, stock={"SKU-1": 1.0}
    )
    assert d.verdict == "not_in_shopify"
    assert d.wrong_company is None


def test_own_company_row_is_unaffected():
    (d,) = _diagnose_in_company([_company_raw(1, 6, enabled=False)], {1}, tenant_company_id=6)
    assert d.verdict == "disabled"


def test_row_without_company_id_is_not_wrong_company():
    (d,) = _diagnose_in_company([_raw(1)], {1}, tenant_company_id=6, stock={"SKU-1": 1.0})
    assert d.verdict == "not_in_shopify"


def test_other_company_rows_do_not_affect_duplicates_or_groups():
    """The real import never sees other companies' rows, so they cannot win a code2."""
    raws = [
        _company_raw(1, 2, name="Other", code2="DUP"),
        _company_raw(2, 6, name="Mine", code2="DUP"),
    ]
    other, mine = _diagnose_in_company(raws, {1, 2}, tenant_company_id=6, stock={"DUP": 1.0})
    assert other.verdict == "wrong_company"
    assert mine.verdict == "not_in_shopify"
    assert mine.duplicate_of is None


async def test_run_diagnose_falls_back_to_every_company_and_finds_other_company_rows(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.bims_company_id = 6
    rec = _install_fakes(
        monkeypatch,
        raw_rows=[_company_raw(1, 2, name="Blue Shirt (M)", code2="SKU-1")],
        stock_by_wh={},
        products=[],
    )
    report = await catalog_ops.run_diagnose(tenant, {"query": "blue shirt"})

    assert "company_id" in rec.catalog_params[0]
    assert "company_id" not in rec.catalog_params[-1]
    assert report["searched_all_companies"] is True
    assert report["total_matches"] == 1
    assert report["rows"][0]["verdict"] == "wrong_company"
    assert report["rows"][0]["wrong_company"] == {"row_company_id": 2, "tenant_company_id": 6}


# -- two-phase company pull, simple mode, configurable timeout ---------------


async def test_phase_two_is_skipped_when_the_tenant_company_matches(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.bims_company_id = 6
    rec = _install_fakes(
        monkeypatch,
        raw_rows=[
            _company_raw(1, 6, name="Jerico Mine", code2="A"),
            _company_raw(2, 2, name="Jerico Other", code2="B"),
        ],
        stock_by_wh={},
        products=[],
    )
    report = await catalog_ops.run_diagnose(tenant, {"query": "jerico"})

    assert report["searched_all_companies"] is False
    assert [r["bims"]["id"] for r in report["rows"]] == [1]
    assert all(p.get("company_id") == 6 for p in rec.catalog_params)


async def test_phase_two_runs_only_when_phase_one_is_empty(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.bims_company_id = 6
    rec = _install_fakes(
        monkeypatch,
        raw_rows=[_company_raw(2, 2, name="Jerico Other", code2="B")],
        stock_by_wh={},
        products=[],
    )
    messages: list[str] = []
    report = await catalog_ops.run_diagnose(
        tenant, {"query": "jerico"}, progress=lambda d, t, m: messages.append(m)
    )

    assert report["searched_all_companies"] is True
    assert report["rows"][0]["verdict"] == "wrong_company"
    # Phase 1: one (empty) page. Phase 2: one page of rows, then the empty page
    # that ends it -- paging stops on an empty page, never on a short one.
    assert [p.get("company_id") for p in rec.catalog_params] == [6, None, None]
    assert "pulling BIMS catalog (all companies)" in messages


async def test_not_found_anywhere_is_reported_as_searched_everywhere(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    _install_fakes(monkeypatch, raw_rows=[_raw(1)], stock_by_wh={}, products=[])
    report = await catalog_ops.run_diagnose(tenant, {"query": "no-such-thing"})
    assert report["total_matches"] == 0
    assert report["searched_all_companies"] is True


async def test_diagnose_requests_simple_mode_in_both_phases(tenant, monkeypatch):
    from bims_shopify.ops import catalog as catalog_ops

    rec = _install_fakes(monkeypatch, raw_rows=[], stock_by_wh={}, products=[])
    await catalog_ops.run_diagnose(tenant, {"query": "x"})
    assert [p["mode"] for p in rec.catalog_params] == ["simple", "simple"]


def test_bims_client_timeout_is_configurable_with_a_conservative_default(tenant):
    from bims_shopify.adapters.bims.client import DEFAULT_TIMEOUT_SECONDS

    assert BIMSClient(tenant)._client.timeout.read == DEFAULT_TIMEOUT_SECONDS == 30.0
    assert BIMSClient(tenant, timeout=7.0)._client.timeout.read == 7.0


async def test_catalog_commands_build_the_bims_client_with_the_catalog_timeout(
    tenant, monkeypatch
):
    from bims_shopify.ops import catalog as catalog_ops

    seen: list[dict] = []
    rec = _install_fakes(monkeypatch, raw_rows=[], stock_by_wh={}, products=[])
    fake_cls = catalog_ops.BIMSClient

    class _Spy(fake_cls):  # type: ignore[misc, valid-type]
        def __init__(self, tenant, **kwargs):
            seen.append(kwargs)
            super().__init__(tenant)

    monkeypatch.setattr(catalog_ops, "BIMSClient", _Spy)
    await catalog_ops.run_diagnose(tenant, {"query": "x"})
    assert seen == [{"timeout": catalog_ops.CATALOG_PULL_TIMEOUT_SECONDS}]
    assert catalog_ops.CATALOG_PULL_TIMEOUT_SECONDS == 120.0
    assert rec.catalog_params
