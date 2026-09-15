"""Tests for the catalog ops script (wipe + import from BIMS)."""
from __future__ import annotations

import httpx
import respx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.ops.catalog import (
    BimsRow,
    apply_import,
    build_product_set_input,
    fetch_existing_skus,
    filter_and_dedupe_rows,
    group_rows,
    split_size_suffix,
    wipe_catalog,
)

GRAPHQL_URL = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
BIMS_URL = "https://bims.example.com"


def _row(bims_id: str, name: str, code2: str, price: float = 1000, enabled=True, exclude=False) -> dict:
    return {
        "id": bims_id,
        "name": name,
        "code2": code2,
        "sell_price": price,
        "enabled": enabled,
        "exclude_ecommerce": exclude,
    }


# -- grouping ----------------------------------------------------------------


def test_split_size_suffix_extracts_trailing_parenthetical():
    assert split_size_suffix("Blue Shirt (M)") == ("Blue Shirt", "M")
    assert split_size_suffix("Sneakers (42)") == ("Sneakers", "42")


def test_split_size_suffix_no_suffix_returns_none():
    assert split_size_suffix("Plain Mug") == ("Plain Mug", None)


def test_filter_and_dedupe_rows_dedupes_by_code2_first_wins():
    raw = [_row("1", "Widget", "SKU-1"), _row("2", "Widget Dup", "SKU-1")]
    rows, duplicates = filter_and_dedupe_rows(raw)
    assert len(rows) == 1
    assert rows[0].bims_id == "1"
    assert len(duplicates) == 1
    assert duplicates[0]["bims_id"] == "2"


def test_filter_and_dedupe_rows_excludes_disabled_and_ecommerce_excluded():
    raw = [
        _row("1", "A", "SKU-A", enabled=False),
        _row("2", "B", "SKU-B", exclude=True),
        _row("3", "C", ""),
        _row("4", "D", "SKU-D"),
    ]
    rows, _ = filter_and_dedupe_rows(raw)
    assert [r.code2 for r in rows] == ["SKU-D"]


def test_group_rows_groups_size_suffixed_rows_into_one_product():
    rows = [
        BimsRow("1", "Shirt (S)", "SKU-S", 1000),
        BimsRow("2", "Shirt (M)", "SKU-M", 1000),
        BimsRow("3", "Shirt (L)", "SKU-L", 1000),
    ]
    report = group_rows(rows)
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.title == "Shirt"
    assert group.has_size_option
    assert {v.sku for v in group.variants} == {"SKU-S", "SKU-M", "SKU-L"}


def test_group_rows_no_suffix_is_single_variant_product():
    rows = [BimsRow("1", "Mug", "SKU-MUG", 5000)]
    report = group_rows(rows)
    assert len(report.groups) == 1
    assert report.groups[0].title == "Mug"
    assert not report.groups[0].has_size_option


def test_group_rows_splits_groups_over_100_variants():
    rows = [BimsRow(str(i), f"Big ({i})", f"SKU-{i}", 100) for i in range(150)]
    report = group_rows(rows)
    assert len(report.split_products) == 1
    assert report.split_products[0]["total_variants"] == 150
    assert report.split_products[0]["parts"] == 2
    titles = [g.title for g in report.groups]
    assert "Big #1" in titles
    assert "Big #2" in titles
    for group in report.groups:
        assert len(group.variants) <= 100


# -- productSet payload shape -------------------------------------------------


def test_build_product_set_input_with_size_option():
    rows = [BimsRow("1", "Shirt (S)", "SKU-S", 1000), BimsRow("2", "Shirt (M)", "SKU-M", 1000)]
    group = group_rows(rows).groups[0]
    payload = build_product_set_input(group, status="DRAFT")
    assert payload["title"] == "Shirt"
    assert payload["status"] == "DRAFT"
    assert payload["productOptions"] == [{"name": "Size", "values": [{"name": "S"}, {"name": "M"}]}]
    assert payload["variants"] == [
        {"sku": "SKU-S", "price": "1000", "optionValues": [{"optionName": "Size", "name": "S"}]},
        {"sku": "SKU-M", "price": "1000", "optionValues": [{"optionName": "Size", "name": "M"}]},
    ]


def test_build_product_set_input_single_variant_no_options():
    group = group_rows([BimsRow("1", "Mug", "SKU-MUG", 5000)]).groups[0]
    payload = build_product_set_input(group, status="ACTIVE")
    assert "productOptions" not in payload
    assert payload["variants"] == [{"sku": "SKU-MUG", "price": "5000"}]


# -- idempotent skip / apply isolation ----------------------------------------


@respx.mock
async def test_fetch_existing_skus_pages_variants(tenant):
    respx.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "productVariants": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {"id": "v1", "sku": "SKU-1", "product": {"id": "p1", "title": "A"}},
                            {"id": "v2", "sku": "", "product": {"id": "p2", "title": "B"}},
                        ],
                    }
                }
            },
        )
    )
    shopify_client = ShopifyClient(tenant)
    skus = await fetch_existing_skus(shopify_client)
    assert skus == {"SKU-1"}


async def test_apply_import_skips_groups_whose_skus_all_exist():
    group = group_rows([BimsRow("1", "Mug", "SKU-MUG", 5000)]).groups[0]

    class _NoCallShopifyClient:
        async def product_set(self, product_input):
            raise AssertionError("product_set should not be called for an existing SKU")

    result = await apply_import(
        _NoCallShopifyClient(), [group], existing_skus={"SKU-MUG"}, status="DRAFT"
    )
    assert result.skipped_existing == ["Mug"]
    assert result.created == []


async def test_apply_import_isolates_failure_per_product():
    group_ok = group_rows([BimsRow("1", "Mug", "SKU-OK", 5000)]).groups[0]
    group_fail = group_rows([BimsRow("2", "Cup", "SKU-FAIL", 5000)]).groups[0]

    from bims_shopify.adapters.shopify.client import ShopifyGraphQLError

    class _FlakyShopifyClient:
        async def product_set(self, product_input):
            if product_input["title"] == "Cup":
                raise ShopifyGraphQLError("boom")
            return {"id": "gid://shopify/Product/1"}

    result = await apply_import(
        _FlakyShopifyClient(), [group_ok, group_fail], existing_skus=set(), status="DRAFT"
    )
    assert len(result.created) == 1
    assert len(result.failed) == 1
    assert result.failed[0]["title"] == "Cup"


# -- wipe safety ---------------------------------------------------------------


async def test_wipe_catalog_deletes_all_and_isolates_failures():
    from bims_shopify.adapters.shopify.client import ShopifyGraphQLError

    class _FakeShopifyClient:
        async def iter_all_products(self):
            for pid in ["p1", "p2", "p3"]:
                yield {"id": pid}

        async def delete_product(self, product_id):
            if product_id == "p2":
                raise ShopifyGraphQLError("boom")

    result = await wipe_catalog(_FakeShopifyClient())
    assert result.deleted == 2
    assert len(result.failed) == 1
    assert result.failed[0]["product_id"] == "p2"


# -- only-with-stock filter (unit-level, via fetch_stock_by_sku shape) --------


@respx.mock
async def test_fetch_stock_by_sku_clamps_negative(tenant):
    from bims_shopify.ops.catalog import fetch_stock_by_sku

    respx.post(f"{BIMS_URL}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(
            200,
            json={"status": "OK", "data": {"stockPorSku": [{"sku": "SKU-1", "stock": -3}]}},
        )
    )
    bims_client = BIMSClient(tenant)
    stock = await fetch_stock_by_sku(bims_client, ["SKU-1"], [1])
    assert stock == {"SKU-1": 0.0}
