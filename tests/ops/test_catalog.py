"""Tests for the catalog ops script (wipe + import + status from BIMS)."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import AuditLogModel
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.config import Settings
from bims_shopify.ops import catalog as catalog_ops
from bims_shopify.ops.catalog import (
    BimsRow,
    apply_dedupe,
    apply_import,
    build_dedupe_report,
    build_product_set_input,
    compute_dedupe_plan,
    fetch_existing_skus,
    filter_and_dedupe_rows,
    group_rows,
    reconciliation_diff,
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


def test_group_rows_dedupes_same_size_duplicates_keeps_first_and_reports_shadowed():
    rows = [
        BimsRow("1", "Shirt (S)", "SKU-S", 1000),
        BimsRow("2", "Shirt (M)", "SKU-M1", 1000),
        BimsRow("3", "Shirt (M)", "SKU-M2", 1000),
        BimsRow("4", "Shirt (L)", "SKU-L", 1000),
    ]
    report = group_rows(rows)
    assert len(report.groups) == 1
    group = report.groups[0]
    # First "(M)" occurrence wins; the duplicate is dropped, not the whole product.
    assert {v.sku for v in group.variants} == {"SKU-S", "SKU-M1", "SKU-L"}
    assert report.shadowed_size_duplicates == [
        {"title": "Shirt", "size": "M", "kept_sku": "SKU-M1", "shadowed_skus": ["SKU-M2"]}
    ]


def test_group_rows_dedupes_default_title_collision_for_no_suffix_rows():
    rows = [
        BimsRow("1", "Mug", "SKU-MUG1", 5000),
        BimsRow("2", "Mug", "SKU-MUG2", 5000),
    ]
    report = group_rows(rows)
    assert len(report.groups) == 1
    group = report.groups[0]
    assert not group.has_size_option
    assert [v.sku for v in group.variants] == ["SKU-MUG1"]
    assert report.shadowed_size_duplicates == [
        {"title": "Mug", "size": "Default", "kept_sku": "SKU-MUG1", "shadowed_skus": ["SKU-MUG2"]}
    ]


def test_group_rows_multi_size_without_duplicates_is_unaffected():
    rows = [
        BimsRow("1", "Shirt (S)", "SKU-S", 1000),
        BimsRow("2", "Shirt (M)", "SKU-M", 1000),
        BimsRow("3", "Shirt (L)", "SKU-L", 1000),
    ]
    report = group_rows(rows)
    assert report.shadowed_size_duplicates == []
    assert {v.sku for v in report.groups[0].variants} == {"SKU-S", "SKU-M", "SKU-L"}


# -- productSet payload shape -------------------------------------------------


def test_build_product_set_input_with_size_option():
    rows = [BimsRow("1", "Shirt (S)", "SKU-S", 1000), BimsRow("2", "Shirt (M)", "SKU-M", 1000)]
    group = group_rows(rows).groups[0]
    payload = build_product_set_input(group, status="DRAFT", location_id="gid://shopify/Location/1")
    assert payload["title"] == "Shirt"
    assert payload["status"] == "DRAFT"
    assert payload["productOptions"] == [{"name": "Size", "values": [{"name": "S"}, {"name": "M"}]}]
    expected_inventory_quantities = [
        {"locationId": "gid://shopify/Location/1", "name": "available", "quantity": 0}
    ]
    assert payload["variants"] == [
        {
            "sku": "SKU-S",
            "price": "1000",
            "optionValues": [{"optionName": "Size", "name": "S"}],
            "inventoryItem": {"tracked": True},
            "inventoryPolicy": "DENY",
            "inventoryQuantities": expected_inventory_quantities,
        },
        {
            "sku": "SKU-M",
            "price": "1000",
            "optionValues": [{"optionName": "Size", "name": "M"}],
            "inventoryItem": {"tracked": True},
            "inventoryPolicy": "DENY",
            "inventoryQuantities": expected_inventory_quantities,
        },
    ]


def test_build_product_set_input_single_variant_no_options():
    group = group_rows([BimsRow("1", "Mug", "SKU-MUG", 5000)]).groups[0]
    payload = build_product_set_input(group, status="ACTIVE", location_id="gid://shopify/Location/1")
    assert payload["productOptions"] == [{"name": "Title", "values": [{"name": "Default Title"}]}]
    assert payload["variants"] == [
        {
            "sku": "SKU-MUG",
            "price": "5000",
            "optionValues": [{"optionName": "Title", "name": "Default Title"}],
            "inventoryItem": {"tracked": True},
            "inventoryPolicy": "DENY",
            "inventoryQuantities": [
                {"locationId": "gid://shopify/Location/1", "name": "available", "quantity": 0}
            ],
        }
    ]


def test_build_product_set_input_without_location_skips_activation_but_tracks():
    group = group_rows([BimsRow("1", "Mug", "SKU-MUG", 5000)]).groups[0]
    payload = build_product_set_input(group, status="ACTIVE", location_id="")
    variant = payload["variants"][0]
    assert variant["inventoryItem"] == {"tracked": True}
    assert "inventoryQuantities" not in variant


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


async def test_apply_import_skips_group_if_any_sku_already_exists():
    """Regression test for the production duplicate-products incident.

    The old rule skipped a group only when ALL its SKUs already existed.
    Because which SKU "survives" per size can shift between runs (bugs
    being fixed mid-rollout), a group could have some-but-not-all SKUs
    present and get re-created as a brand new product, duplicating it.
    """
    group = group_rows(
        [
            BimsRow("1", "Shirt (S)", "SKU-S", 1000),
            BimsRow("2", "Shirt (M)", "SKU-M", 1000),
        ]
    ).groups[0]

    class _NoCallShopifyClient:
        async def product_set(self, product_input):
            raise AssertionError("product_set should not be called when any SKU already exists")

    # Only SKU-S exists in Shopify; SKU-M does not. The old subset rule would
    # have re-created this product. The fixed rule must still skip it.
    result = await apply_import(
        _NoCallShopifyClient(), [group], existing_skus={"SKU-S"}, status="DRAFT"
    )
    assert result.skipped_existing == ["Shirt"]
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


# -- reconciliation -------------------------------------------------------


def test_reconciliation_diff_finds_skus_missing_from_shopify():
    result = reconciliation_diff({"A", "B", "C"}, {"B"})
    assert result["skus_in_bims_not_in_shopify"] == {"count": 2, "sample": ["A", "C"]}
    assert result["matched_skus"] == 1


def test_reconciliation_diff_finds_skus_missing_from_bims():
    result = reconciliation_diff({"A"}, {"A", "B", "C"})
    assert result["skus_in_shopify_not_in_bims"] == {"count": 2, "sample": ["B", "C"]}
    assert result["matched_skus"] == 1


def test_reconciliation_diff_all_matched_when_sets_equal():
    result = reconciliation_diff({"A", "B"}, {"A", "B"})
    assert result["skus_in_bims_not_in_shopify"] == {"count": 0, "sample": []}
    assert result["skus_in_shopify_not_in_bims"] == {"count": 0, "sample": []}
    assert result["matched_skus"] == 2


def test_reconciliation_diff_caps_sample_at_20():
    bims_only = {f"SKU-{i}" for i in range(25)}
    result = reconciliation_diff(bims_only, set())
    assert result["skus_in_bims_not_in_shopify"]["count"] == 25
    assert len(result["skus_in_bims_not_in_shopify"]["sample"]) == 20


# -- status subcommand: end-to-end --------------------------------------------


async def _make_status_db(monkeypatch, tmp_path) -> Settings:
    db_path = tmp_path / "status.db"
    settings = Settings(
        admin_token="test-admin-token",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_path}",
    )

    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = settings.database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    monkeypatch.setattr(catalog_ops, "get_settings", lambda: settings)
    return settings


async def _seed_tenant(settings: Settings, tenant) -> int:
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
            created = await repo.create(tenant.__class__(**{**tenant.__dict__, "id": None}))
            return created.id
    finally:
        await engine.dispose()


@respx.mock
async def test_run_status_prints_full_reconciliation_report(monkeypatch, tmp_path, tenant, capsys):
    settings = await _make_status_db(monkeypatch, tmp_path)
    tenant_id = await _seed_tenant(settings, tenant)
    tenant.id = tenant_id

    respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "count": "2",
                "data": [
                    {"Product": _row("1", "Shirt (S)", "SKU-S")},
                    {"Product": _row("2", "Mug", "SKU-ORPHAN")},
                ],
            },
        )
    )
    respx.post(GRAPHQL_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "products": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"id": "p1", "title": "Shirt"}],
                        }
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {
                        "productVariants": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {"id": "v1", "sku": "SKU-S", "product": {"id": "p1", "title": "Shirt"}},
                                {"id": "v2", "sku": "SKU-EXTRA", "product": {"id": "p1", "title": "Shirt"}},
                            ],
                        }
                    }
                },
            ),
        ]
    )

    # main() itself just wraps `_run` in asyncio.run() + json.dumps(..., indent=2)
    # (see bims_shopify.ops.catalog.main); calling it directly here would nest
    # asyncio.run() inside pytest-asyncio's already-running loop, so we drive
    # the same code path it uses and print exactly as it would.
    args = catalog_ops._build_arg_parser().parse_args(["acme", "status"])
    tenant_from_db = await catalog_ops._load_tenant_from_db(args.tenant_slug)
    report = await catalog_ops._run_status(tenant_from_db, args)
    print(json.dumps(report, indent=2))
    out = capsys.readouterr().out
    report = json.loads(out)

    assert report["bims"] == {"eligible_products": 2, "eligible_variants": 2, "duplicate_code2": 0}
    assert report["shopify"] == {"products": 1, "variants": 2, "variants_with_sku": 2}
    assert report["reconciliation"]["matched_skus"] == 1
    assert report["reconciliation"]["skus_in_bims_not_in_shopify"]["sample"] == ["SKU-ORPHAN"]
    assert report["reconciliation"]["skus_in_shopify_not_in_bims"]["sample"] == ["SKU-EXTRA"]
    assert report["last_activity"] == {
        "last_import": None,
        "last_wipe": None,
        "last_sync": {"last_run_at": None, "last_run_summary": {}},
    }

    # Persisted for the admin endpoint to serve back — counts only, no samples.
    engine = create_async_engine(settings.database_url, future=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await session.execute(
                select(AuditLogModel).where(AuditLogModel.action == "catalog.reconciliation")
            )
            entries = list(result.scalars().all())
    finally:
        await engine.dispose()
    assert len(entries) == 1
    assert "sample" not in json.dumps(entries[0].payload)
    assert entries[0].payload["reconciliation"]["matched_skus"] == 1


# -- dedupe --------------------------------------------------------------


def test_compute_dedupe_plan_marks_full_duplicate_for_deletion():
    products = [
        {"id": "p1", "title": "Shirt", "skus": ["SKU-S", "SKU-M"]},
        {"id": "p2", "title": "Shirt", "skus": ["SKU-S", "SKU-M"]},
    ]
    plan = compute_dedupe_plan(products)
    assert plan.total_products == 2
    assert plan.kept == 1
    assert [d["product_id"] for d in plan.duplicates_to_delete] == ["p2"]
    assert plan.duplicates_to_delete[0]["skus"] == ["SKU-M", "SKU-S"]
    assert plan.partial_overlap == []


def test_compute_dedupe_plan_keeps_and_reports_partial_overlap():
    products = [
        {"id": "p1", "title": "Shirt", "skus": ["SKU-S", "SKU-M"]},
        # Shares SKU-M with p1 but also introduces SKU-L -> not a full
        # duplicate, must be kept, but flagged for manual review.
        {"id": "p2", "title": "Shirt Variant", "skus": ["SKU-M", "SKU-L"]},
    ]
    plan = compute_dedupe_plan(products)
    assert plan.kept == 2
    assert plan.duplicates_to_delete == []
    assert len(plan.partial_overlap) == 1
    assert plan.partial_overlap[0]["product_id"] == "p2"
    assert plan.partial_overlap[0]["overlapping_skus"] == ["SKU-M"]


def test_compute_dedupe_plan_keeps_products_with_no_skus():
    products = [{"id": "p1", "title": "Empty", "skus": []}]
    plan = compute_dedupe_plan(products)
    assert plan.kept == 1
    assert plan.duplicates_to_delete == []


def test_compute_dedupe_plan_second_pass_finds_no_duplicates():
    """Idempotency: running dedupe again after duplicates were removed finds none."""
    products = [
        {"id": "p1", "title": "Shirt", "skus": ["SKU-S", "SKU-M"]},
        {"id": "p3", "title": "Mug", "skus": ["SKU-MUG"]},
    ]
    plan = compute_dedupe_plan(products)
    assert plan.duplicates_to_delete == []
    assert plan.kept == 2


def test_build_dedupe_report_shape_and_sample_caps():
    duplicates = [
        {"product_id": f"p{i}", "title": "X", "skus": [f"SKU-{i}"]} for i in range(25)
    ]
    plan_products = [{"id": "p-keep", "title": "Keep", "skus": ["SKU-KEEP"]}] + [
        {"id": d["product_id"], "title": d["title"], "skus": ["SKU-KEEP"]} for d in duplicates
    ]
    plan = compute_dedupe_plan(plan_products)
    report = build_dedupe_report(plan)
    assert report["total_products"] == 26
    assert report["kept"] == 1
    assert report["duplicates_to_delete"]["count"] == 25
    assert len(report["duplicates_to_delete"]["sample"]) == 20
    assert report["partial_overlap"]["count"] == 0
    assert report["partial_overlap"]["sample"] == []


async def test_apply_dedupe_deletes_and_isolates_failures():
    from bims_shopify.adapters.shopify.client import ShopifyGraphQLError

    class _FakeShopifyClient:
        async def delete_product(self, product_id):
            if product_id == "p2":
                raise ShopifyGraphQLError("boom")

    duplicates = [
        {"product_id": "p1", "title": "A", "skus": []},
        {"product_id": "p2", "title": "B", "skus": []},
    ]
    result = await apply_dedupe(_FakeShopifyClient(), duplicates)
    assert result.deleted == 1
    assert len(result.failed) == 1
    assert result.failed[0]["product_id"] == "p2"


async def test_run_dedupe_dry_run_reports_without_deleting(tenant):
    from bims_shopify.ops import catalog as catalog_ops

    class _FakeShopifyClient:
        def __init__(self, tenant):
            pass

        async def iter_all_products_with_skus(self):
            for pid, skus in [
                ("p1", ["SKU-A"]),
                ("p2", ["SKU-A"]),  # full duplicate of p1
            ]:
                yield {"id": pid, "title": "Widget", "skus": skus}

        async def aclose(self):
            pass

    monkeypatch_target = catalog_ops.ShopifyClient
    catalog_ops.ShopifyClient = _FakeShopifyClient  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(["acme", "dedupe"])
        report = await catalog_ops._run_dedupe(tenant, args)
    finally:
        catalog_ops.ShopifyClient = monkeypatch_target  # type: ignore[assignment]

    assert report["dry_run"] is True
    assert report["duplicates_to_delete"]["count"] == 1
    assert "apply" not in report


async def test_run_dedupe_safety_guard_blocks_over_60_percent(tenant):
    from bims_shopify.ops import catalog as catalog_ops

    class _FakeShopifyClient:
        def __init__(self, tenant):
            pass

        async def iter_all_products_with_skus(self):
            # 1 kept, 4 duplicates -> 80% of the catalog, over the 60% guard.
            yield {"id": "p0", "title": "Base", "skus": ["SKU-A"]}
            for i in range(1, 5):
                yield {"id": f"p{i}", "title": "Base", "skus": ["SKU-A"]}

        async def aclose(self):
            pass

    monkeypatch_target = catalog_ops.ShopifyClient
    catalog_ops.ShopifyClient = _FakeShopifyClient  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(["acme", "dedupe", "--apply"])
        try:
            await catalog_ops._run_dedupe(tenant, args)
        except SystemExit as exc:
            assert "Safety guard" in str(exc)
        else:
            raise AssertionError("expected SystemExit from the safety guard")
    finally:
        catalog_ops.ShopifyClient = monkeypatch_target  # type: ignore[assignment]


# -- fix_tracking (repair legacy untracked variants) --------------------------


def _fix_tracking_fake_client(variants: list[dict], calls: dict):
    class _FakeShopifyClient:
        def __init__(self, tenant):
            pass

        async def iter_all_variants(self):
            for variant in variants:
                yield variant

        async def bulk_update_variants(self, product_id, bulk_variants):
            calls.setdefault("bulk_update", []).append((product_id, bulk_variants))

        async def activate_inventory_item(self, inventory_item_id, location_id):
            calls.setdefault("activate", []).append((inventory_item_id, location_id))

        async def aclose(self):
            pass

    return _FakeShopifyClient


def _variant(sku, product_id, tracked, inventory_item_id="inv-1", variant_id=None):
    return {
        "id": variant_id or f"gid://shopify/ProductVariant/{sku}",
        "sku": sku,
        "product": {"id": product_id, "title": "Widget"},
        "inventoryItem": {"id": inventory_item_id, "tracked": tracked},
    }


async def test_run_fix_tracking_dry_run_reports_without_mutating(tenant):
    from bims_shopify.ops import catalog as catalog_ops

    variants = [
        _variant("SKU-A", "p1", tracked=False),
        _variant("SKU-B", "p1", tracked=False),
        _variant("SKU-C", "p2", tracked=True),
    ]
    calls: dict = {}
    monkeypatch_target = catalog_ops.ShopifyClient
    catalog_ops.ShopifyClient = _fix_tracking_fake_client(variants, calls)  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(["acme", "fix-tracking"])
        report = await catalog_ops._run_fix_tracking(tenant, args)
    finally:
        catalog_ops.ShopifyClient = monkeypatch_target  # type: ignore[assignment]

    assert report["dry_run"] is True
    assert report["checked"] == 3
    assert report["already_ok"] == 1
    assert report["fixed"] == 2
    assert report["products_to_fix"] == 1
    assert "bulk_update" not in calls
    assert "activate" not in calls


async def test_run_fix_tracking_apply_fixes_untracked_variants_and_is_idempotent(tenant):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.id = None  # skip the real-DB audit write; audit logging is exercised elsewhere
    variants = [
        _variant("SKU-A", "p1", tracked=False, inventory_item_id="inv-a"),
        _variant("SKU-B", "p1", tracked=False, inventory_item_id="inv-b"),
        _variant("SKU-C", "p2", tracked=True, inventory_item_id="inv-c"),
    ]
    calls: dict = {}
    monkeypatch_target = catalog_ops.ShopifyClient
    catalog_ops.ShopifyClient = _fix_tracking_fake_client(variants, calls)  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(["acme", "fix-tracking", "--apply"])
        summary = await catalog_ops._run_fix_tracking(tenant, args)
    finally:
        catalog_ops.ShopifyClient = monkeypatch_target  # type: ignore[assignment]

    assert summary == {"checked": 3, "already_ok": 1, "fixed": 2, "failed": []}
    assert len(calls["bulk_update"]) == 1
    product_id, bulk_variants = calls["bulk_update"][0]
    assert product_id == "p1"
    assert bulk_variants == [
        {"id": "gid://shopify/ProductVariant/SKU-A", "inventoryItem": {"tracked": True}},
        {"id": "gid://shopify/ProductVariant/SKU-B", "inventoryItem": {"tracked": True}},
    ]
    assert set(calls["activate"]) == {
        ("inv-a", tenant.shopify_location_id),
        ("inv-b", tenant.shopify_location_id),
    }

    # Second run: everything now reports tracked=True -> 0 fixed (idempotent).
    variants_after = [
        _variant("SKU-A", "p1", tracked=True, inventory_item_id="inv-a"),
        _variant("SKU-B", "p1", tracked=True, inventory_item_id="inv-b"),
        _variant("SKU-C", "p2", tracked=True, inventory_item_id="inv-c"),
    ]
    calls_2: dict = {}
    catalog_ops.ShopifyClient = _fix_tracking_fake_client(variants_after, calls_2)  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(["acme", "fix-tracking", "--apply"])
        summary_2 = await catalog_ops._run_fix_tracking(tenant, args)
    finally:
        catalog_ops.ShopifyClient = monkeypatch_target  # type: ignore[assignment]

    assert summary_2 == {"checked": 3, "already_ok": 3, "fixed": 0, "failed": []}
    assert "bulk_update" not in calls_2


async def test_run_fix_tracking_isolates_per_product_failures(tenant):
    from bims_shopify.ops import catalog as catalog_ops
    from bims_shopify.adapters.shopify.client import ShopifyGraphQLError

    tenant.id = None  # skip the real-DB audit write; audit logging is exercised elsewhere
    variants = [
        _variant("SKU-A", "p1", tracked=False, inventory_item_id="inv-a"),
        _variant("SKU-B", "p2", tracked=False, inventory_item_id="inv-b"),
    ]

    class _FailingFakeClient:
        def __init__(self, tenant):
            pass

        async def iter_all_variants(self):
            for variant in variants:
                yield variant

        async def bulk_update_variants(self, product_id, bulk_variants):
            if product_id == "p1":
                raise ShopifyGraphQLError("boom")

        async def activate_inventory_item(self, inventory_item_id, location_id):
            pass

        async def aclose(self):
            pass

    monkeypatch_target = catalog_ops.ShopifyClient
    catalog_ops.ShopifyClient = _FailingFakeClient  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(["acme", "fix-tracking", "--apply"])
        summary = await catalog_ops._run_fix_tracking(tenant, args)
    finally:
        catalog_ops.ShopifyClient = monkeypatch_target  # type: ignore[assignment]

    assert summary["fixed"] == 1
    assert summary["failed"] == [{"product_id": "p1", "error": "ShopifyGraphQLError: boom"}]


# -- cleanup_no_stock (draft/delete zero-stock products) ----------------------


def test_compute_cleanup_plan_all_zero_stock_is_target():
    from bims_shopify.ops.catalog import compute_cleanup_plan

    products = [{"id": "p1", "title": "Dead Shirt", "status": "ACTIVE", "skus": ["SKU-A", "SKU-B"]}]
    stock = {"SKU-A": 0.0, "SKU-B": 0.0}
    plan = compute_cleanup_plan(products, stock, mode="draft")
    assert [t["product_id"] for t in plan.targets] == ["p1"]
    assert plan.unknown == []
    assert plan.already_done == 0


def test_compute_cleanup_plan_any_positive_stock_is_kept():
    from bims_shopify.ops.catalog import compute_cleanup_plan

    products = [{"id": "p1", "title": "Live Shirt", "status": "ACTIVE", "skus": ["SKU-A", "SKU-B"]}]
    stock = {"SKU-A": 0.0, "SKU-B": 3.0}
    plan = compute_cleanup_plan(products, stock, mode="draft")
    assert plan.targets == []
    assert plan.unknown == []
    assert plan.already_done == 0


def test_compute_cleanup_plan_unknown_sku_is_kept_and_reported():
    from bims_shopify.ops.catalog import compute_cleanup_plan

    products = [{"id": "p1", "title": "Mystery", "status": "ACTIVE", "skus": ["SKU-A", "SKU-GONE"]}]
    stock = {"SKU-A": 0.0}  # SKU-GONE has no BIMS record at all
    plan = compute_cleanup_plan(products, stock, mode="draft")
    assert plan.targets == []
    assert [u["product_id"] for u in plan.unknown] == ["p1"]


def test_compute_cleanup_plan_no_skus_is_unknown():
    from bims_shopify.ops.catalog import compute_cleanup_plan

    products = [{"id": "p1", "title": "No Variants", "status": "ACTIVE", "skus": []}]
    plan = compute_cleanup_plan(products, {}, mode="draft")
    assert plan.targets == []
    assert [u["product_id"] for u in plan.unknown] == ["p1"]


def test_compute_cleanup_plan_draft_mode_already_done_is_skipped():
    from bims_shopify.ops.catalog import compute_cleanup_plan

    products = [{"id": "p1", "title": "Already Drafted", "status": "DRAFT", "skus": ["SKU-A"]}]
    plan = compute_cleanup_plan(products, {"SKU-A": 0.0}, mode="draft")
    assert plan.targets == []
    assert plan.already_done == 1


def test_compute_cleanup_plan_delete_mode_has_no_already_done():
    from bims_shopify.ops.catalog import compute_cleanup_plan

    products = [{"id": "p1", "title": "Zero Stock", "status": "DRAFT", "skus": ["SKU-A"]}]
    plan = compute_cleanup_plan(products, {"SKU-A": 0.0}, mode="delete")
    assert [t["product_id"] for t in plan.targets] == ["p1"]
    assert plan.already_done == 0


def test_build_cleanup_report_shape_and_sample_caps():
    from bims_shopify.ops.catalog import CleanupPlan, build_cleanup_report

    plan = CleanupPlan(
        total_products=30,
        targets=[{"product_id": f"p{i}", "title": "X", "skus": []} for i in range(25)],
        unknown=[{"product_id": f"u{i}", "title": "Y", "skus": []} for i in range(12)],
        already_done=3,
    )
    report = build_cleanup_report(plan)
    assert report["total_products"] == 30
    assert report["targets"]["count"] == 25
    assert len(report["targets"]["sample"]) == 20
    assert report["unknown"]["count"] == 12
    assert len(report["unknown"]["sample"]) == 10
    assert report["already_done"] == 3


async def test_apply_cleanup_draft_mode_sets_status_and_isolates_failures():
    from bims_shopify.adapters.shopify.client import ShopifyGraphQLError
    from bims_shopify.ops.catalog import apply_cleanup

    calls = []

    class _FakeShopifyClient:
        async def set_product_status(self, product_id, status):
            if product_id == "p2":
                raise ShopifyGraphQLError("boom")
            calls.append((product_id, status))

    targets = [
        {"product_id": "p1", "title": "A", "skus": []},
        {"product_id": "p2", "title": "B", "skus": []},
    ]
    result = await apply_cleanup(_FakeShopifyClient(), targets, mode="draft")
    assert result.processed == 1
    assert calls == [("p1", "DRAFT")]
    assert result.failed == [{"product_id": "p2", "error": "ShopifyGraphQLError: boom"}]


async def test_apply_cleanup_delete_mode_deletes_products():
    from bims_shopify.ops.catalog import apply_cleanup

    calls = []

    class _FakeShopifyClient:
        async def delete_product(self, product_id):
            calls.append(product_id)

    targets = [{"product_id": "p1", "title": "A", "skus": []}]
    result = await apply_cleanup(_FakeShopifyClient(), targets, mode="delete")
    assert result.processed == 1
    assert calls == ["p1"]


def _cleanup_fake_clients(products: list[dict], stock_by_sku: dict[str, float], calls: dict):
    class _FakeShopifyClient:
        def __init__(self, tenant):
            pass

        async def iter_all_products_with_skus(self):
            for product in products:
                yield product

        async def set_product_status(self, product_id, status):
            calls.setdefault("status", []).append((product_id, status))

        async def delete_product(self, product_id):
            calls.setdefault("deleted", []).append(product_id)

        async def aclose(self):
            pass

    class _FakeBimsClient:
        def __init__(self, tenant):
            pass

        async def stock_fenicio(self, *, skus, warehouse_ids, request_id):
            return {
                "data": {
                    "stockPorSku": [
                        {"sku": sku, "stock": stock_by_sku[sku]}
                        for sku in skus
                        if sku in stock_by_sku
                    ]
                }
            }

        async def aclose(self):
            pass

    return _FakeShopifyClient, _FakeBimsClient


async def test_run_cleanup_no_stock_dry_run_reports_without_mutating(tenant):
    from bims_shopify.ops import catalog as catalog_ops

    products = [
        {"id": "p1", "title": "Dead", "status": "ACTIVE", "skus": ["SKU-A"]},
        {"id": "p2", "title": "Live", "status": "ACTIVE", "skus": ["SKU-B"]},
    ]
    stock = {"SKU-A": 0.0, "SKU-B": 5.0}
    calls: dict = {}
    fake_shopify, fake_bims = _cleanup_fake_clients(products, stock, calls)

    shopify_target = catalog_ops.ShopifyClient
    bims_target = catalog_ops.BIMSClient
    catalog_ops.ShopifyClient = fake_shopify  # type: ignore[assignment]
    catalog_ops.BIMSClient = fake_bims  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(["acme", "cleanup-no-stock"])
        report = await catalog_ops._run_cleanup_no_stock(tenant, args)
    finally:
        catalog_ops.ShopifyClient = shopify_target  # type: ignore[assignment]
        catalog_ops.BIMSClient = bims_target  # type: ignore[assignment]

    assert report["dry_run"] is True
    assert report["mode"] == "draft"
    assert report["targets"]["count"] == 1
    assert report["targets"]["sample"][0]["product_id"] == "p1"
    assert "apply" not in calls
    assert calls == {}


async def test_run_cleanup_no_stock_apply_draft_mode_sets_status(tenant):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.id = None  # skip real-DB audit write
    products = [{"id": "p1", "title": "Dead", "status": "ACTIVE", "skus": ["SKU-A"]}]
    stock = {"SKU-A": 0.0}
    calls: dict = {}
    fake_shopify, fake_bims = _cleanup_fake_clients(products, stock, calls)

    shopify_target = catalog_ops.ShopifyClient
    bims_target = catalog_ops.BIMSClient
    catalog_ops.ShopifyClient = fake_shopify  # type: ignore[assignment]
    catalog_ops.BIMSClient = fake_bims  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(
            ["acme", "cleanup-no-stock", "--apply", "--force"]
        )
        report = await catalog_ops._run_cleanup_no_stock(tenant, args)
    finally:
        catalog_ops.ShopifyClient = shopify_target  # type: ignore[assignment]
        catalog_ops.BIMSClient = bims_target  # type: ignore[assignment]

    assert report["apply"]["processed"] == 1
    assert calls["status"] == [("p1", "DRAFT")]


async def test_run_cleanup_no_stock_apply_delete_mode_deletes(tenant):
    from bims_shopify.ops import catalog as catalog_ops

    tenant.id = None
    products = [{"id": "p1", "title": "Dead", "status": "ACTIVE", "skus": ["SKU-A"]}]
    stock = {"SKU-A": 0.0}
    calls: dict = {}
    fake_shopify, fake_bims = _cleanup_fake_clients(products, stock, calls)

    shopify_target = catalog_ops.ShopifyClient
    bims_target = catalog_ops.BIMSClient
    catalog_ops.ShopifyClient = fake_shopify  # type: ignore[assignment]
    catalog_ops.BIMSClient = fake_bims  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(
            ["acme", "cleanup-no-stock", "--apply", "--mode", "delete", "--force"]
        )
        report = await catalog_ops._run_cleanup_no_stock(tenant, args)
    finally:
        catalog_ops.ShopifyClient = shopify_target  # type: ignore[assignment]
        catalog_ops.BIMSClient = bims_target  # type: ignore[assignment]

    assert report["apply"]["processed"] == 1
    assert calls["deleted"] == ["p1"]


async def test_run_cleanup_no_stock_idempotent_second_pass_finds_no_new_targets(tenant):
    """Draft mode: once a product is DRAFT, the next dry run reports it as already_done."""
    from bims_shopify.ops import catalog as catalog_ops

    tenant.id = None
    products_first_pass = [{"id": "p1", "title": "Dead", "status": "ACTIVE", "skus": ["SKU-A"]}]
    stock = {"SKU-A": 0.0}
    calls: dict = {}
    fake_shopify, fake_bims = _cleanup_fake_clients(products_first_pass, stock, calls)

    shopify_target = catalog_ops.ShopifyClient
    bims_target = catalog_ops.BIMSClient
    catalog_ops.ShopifyClient = fake_shopify  # type: ignore[assignment]
    catalog_ops.BIMSClient = fake_bims  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(
            ["acme", "cleanup-no-stock", "--apply", "--force"]
        )
        first = await catalog_ops._run_cleanup_no_stock(tenant, args)
        assert first["apply"]["processed"] == 1

        # Second pass: same product now reports DRAFT (simulating the applied change).
        products_second_pass = [{"id": "p1", "title": "Dead", "status": "DRAFT", "skus": ["SKU-A"]}]
        fake_shopify_2, fake_bims_2 = _cleanup_fake_clients(products_second_pass, stock, {})
        catalog_ops.ShopifyClient = fake_shopify_2  # type: ignore[assignment]
        catalog_ops.BIMSClient = fake_bims_2  # type: ignore[assignment]
        second_args = catalog_ops._build_arg_parser().parse_args(["acme", "cleanup-no-stock"])
        second = await catalog_ops._run_cleanup_no_stock(tenant, second_args)
    finally:
        catalog_ops.ShopifyClient = shopify_target  # type: ignore[assignment]
        catalog_ops.BIMSClient = bims_target  # type: ignore[assignment]

    assert second["targets"]["count"] == 0
    assert second["already_done"] == 1


async def test_run_cleanup_no_stock_safety_guard_blocks_over_60_percent(tenant):
    from bims_shopify.ops import catalog as catalog_ops

    products = [{"id": "p0", "title": "Live", "status": "ACTIVE", "skus": ["SKU-LIVE"]}]
    stock = {"SKU-LIVE": 5.0}
    for i in range(1, 5):
        products.append({"id": f"p{i}", "title": "Dead", "status": "ACTIVE", "skus": [f"SKU-{i}"]})
        stock[f"SKU-{i}"] = 0.0
    calls: dict = {}
    fake_shopify, fake_bims = _cleanup_fake_clients(products, stock, calls)

    shopify_target = catalog_ops.ShopifyClient
    bims_target = catalog_ops.BIMSClient
    catalog_ops.ShopifyClient = fake_shopify  # type: ignore[assignment]
    catalog_ops.BIMSClient = fake_bims  # type: ignore[assignment]
    try:
        args = catalog_ops._build_arg_parser().parse_args(["acme", "cleanup-no-stock", "--apply"])
        try:
            await catalog_ops._run_cleanup_no_stock(tenant, args)
        except SystemExit as exc:
            assert "Safety guard" in str(exc)
        else:
            raise AssertionError("expected SystemExit from the safety guard")
        assert calls == {}
    finally:
        catalog_ops.ShopifyClient = shopify_target  # type: ignore[assignment]
        catalog_ops.BIMSClient = bims_target  # type: ignore[assignment]


# -- incremental import via BIMS last_update ----------------------------------


@respx.mock
async def test_fetch_bims_catalog_rows_full_pull_when_since_none(tenant):
    from bims_shopify.ops.catalog import fetch_bims_catalog_rows

    route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )
    bims_client = BIMSClient(tenant)
    rows = await fetch_bims_catalog_rows(bims_client, tenant.bims_company_id, since=None)

    assert rows == []
    assert "last_update" not in route.calls[0].request.url.params


@respx.mock
async def test_fetch_bims_catalog_rows_incremental_passes_last_update_in_bims_local_format(tenant):
    from bims_shopify.adapters.bims.timezones import to_bims_local
    from bims_shopify.ops.catalog import fetch_bims_catalog_rows

    route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )
    since = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    bims_client = BIMSClient(tenant)
    await fetch_bims_catalog_rows(
        bims_client, tenant.bims_company_id, since=since, tenant_timezone=tenant.bims_timezone
    )

    sent = route.calls[0].request.url.params
    assert sent["last_update"] == to_bims_local(since, tenant.bims_timezone)


async def test_parse_since_returns_none_for_missing():
    assert catalog_ops._parse_since(None) is None
    assert catalog_ops._parse_since("") is None


async def test_parse_since_rejects_naive_datetime():
    with pytest.raises(ValueError, match="aware"):
        catalog_ops._parse_since("2026-09-20T12:00:00")


async def test_parse_since_accepts_aware_iso():
    since = catalog_ops._parse_since("2026-09-20T12:00:00+00:00")
    assert since is not None
    assert since.tzinfo is not None


@respx.mock
async def test_run_import_incremental_dry_run_passes_last_update_and_flags_incremental(tenant):
    from bims_shopify.adapters.bims.timezones import to_bims_local

    since = datetime(2026, 9, 24, 8, 0, 0, tzinfo=UTC)
    index_route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "data": [{"Product": _row("1", "Mug", "SKU-MUG")}]},
        )
    )

    summary = await catalog_ops.run_import(
        tenant, {"apply": False, "since_iso": since.isoformat()}
    )

    assert summary["incremental"] is True
    assert summary["dry_run"] is True
    assert summary["products"] == 1
    sent = index_route.calls[0].request.url.params
    assert sent["last_update"] == to_bims_local(since, tenant.bims_timezone)


@respx.mock
async def test_run_import_manual_default_stays_full_no_last_update(tenant):
    index_route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "data": [{"Product": _row("1", "Mug", "SKU-MUG")}]},
        )
    )

    # Manual import options never include since_iso unless a caller
    # explicitly passes it -- must stay a full pull.
    summary = await catalog_ops.run_import(tenant, {"apply": False})

    assert summary["incremental"] is False
    assert "last_update" not in index_route.calls[0].request.url.params


@respx.mock
async def test_run_import_incremental_still_respects_only_with_stock(tenant):
    since = datetime(2026, 9, 24, 8, 0, 0, tzinfo=UTC)
    respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "data": [
                    {"Product": _row("1", "InStock", "SKU-IN")},
                    {"Product": _row("2", "OutOfStock", "SKU-OUT")},
                ],
            },
        )
    )
    respx.post(f"{BIMS_URL}/api/products_stocks/stock_fenicio.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "data": {"stockPorSku": [{"sku": "SKU-IN", "stock": 5}, {"sku": "SKU-OUT", "stock": 0}]},
            },
        )
    )

    summary = await catalog_ops.run_import(
        tenant,
        {"apply": False, "since_iso": since.isoformat(), "only_with_stock": True},
    )

    assert summary["incremental"] is True
    assert summary["products"] == 1
    assert summary["sample"][0]["skus"] == ["SKU-IN"]
