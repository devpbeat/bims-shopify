"""The scan: stream the Shopify catalog, keep only products with no media."""
from __future__ import annotations

from bims_shopify.application.products_without_photos import (
    build_view,
    scan_products_without_photos,
)

ONLINE = "gid://shopify/Publication/1"


def _product(n: int, *, media: int = 0, status: str = "ACTIVE", skus=None, published=None):
    return {
        "id": f"gid://shopify/Product/{n}",
        "title": f"Product {n}",
        "status": status,
        "skus": [f"SKU-{n}"] if skus is None else skus,
        "media_count": media,
        "published_publication_ids": [ONLINE] if published is None else published,
    }


async def _stream(products):
    for product in products:
        yield product


async def _scan(products, *, stock=None, publication_id=ONLINE, row_cap=100, progress=None):
    return await scan_products_without_photos(
        _stream(products),
        publication_id=publication_id,
        stock_by_sku=stock or {},
        row_cap=row_cap,
        progress=progress,
    )


async def test_only_products_with_zero_media_are_kept():
    scan = await _scan([_product(1, media=0), _product(2, media=3), _product(3, media=0)])

    assert [r.product_id[-1] for r in scan.rows] == ["1", "3"]
    assert scan.scanned == 3
    assert scan.without_photos == 2


async def test_row_carries_what_the_operator_needs_to_act():
    scan = await _scan(
        [_product(1, status="DRAFT", published=[])], stock={"SKU-1": 7.0}
    )

    (row,) = scan.rows
    assert row.skus == ("SKU-1",)
    assert row.title == "Product 1"
    assert row.status == "DRAFT"
    assert row.on_online_store is False
    assert row.stock == 7.0


async def test_stock_sums_the_known_skus_and_is_none_when_none_is_known():
    scan = await _scan(
        [_product(1, skus=["A", "B"]), _product(2, skus=["C"]), _product(3, skus=[])],
        stock={"A": 2.0, "B": 3.0},
    )

    assert [r.stock for r in scan.rows] == [5.0, None, None]


async def test_unknown_publication_leaves_on_online_store_unknown():
    scan = await _scan([_product(1)], publication_id=None)

    assert scan.rows[0].on_online_store is None


async def test_live_count_is_exact_and_counts_active_on_store_products_only():
    scan = await _scan(
        [_product(1), _product(2, status="DRAFT"), _product(3, published=[]), _product(4, media=1)]
    )

    assert scan.without_photos == 3
    assert scan.live_without_photos == 1


async def test_row_cap_bounds_memory_but_counts_stay_exact():
    scan = await _scan([_product(n) for n in range(1, 8)], row_cap=3)

    assert len(scan.rows) == 3
    assert scan.truncated is True
    assert scan.without_photos == 7
    assert scan.live_without_photos == 7


async def test_under_the_cap_is_not_truncated():
    scan = await _scan([_product(1)], row_cap=3)

    assert scan.truncated is False


async def test_progress_is_reported_while_scanning():
    seen: list[tuple[int, str]] = []

    async def progress(done, total, message):
        seen.append((done, message))

    await _scan([_product(n) for n in range(1, 4)], progress=progress)

    assert seen[-1][0] == 3
    assert all(isinstance(message, str) and message for _, message in seen)


async def test_stored_form_is_json_serializable_and_round_trips_through_the_view():
    import json

    scan = await _scan([_product(1), _product(2, status="DRAFT")], stock={"SKU-1": 4.0})
    stored = json.loads(json.dumps(scan.to_dict()))

    view = build_view(
        stored, scope="all", query="", page=1, page_size=50, shop_domain="acme.myshopify.com"
    )

    assert view["counts"] == {"live": 1, "all": 2, "scanned": 2}
    assert [i["status"] for i in view["items"]] == ["ACTIVE", "DRAFT"]
    assert view["items"][0]["admin_url"] == "https://acme.myshopify.com/admin/products/1"
    assert view["items"][0]["stock"] == 4.0
    assert view["truncated"] is False


def test_view_paginates_and_reports_the_filtered_total():
    rows = [
        {
            "product_id": f"gid://shopify/Product/{n}",
            "title": f"P{n}",
            "skus": [f"S{n}"],
            "status": "ACTIVE",
            "on_online_store": True,
            "stock": None,
        }
        for n in range(1, 8)
    ]
    stored = {
        "rows": rows,
        "scanned": 20,
        "without_photos": 7,
        "live_without_photos": 7,
        "truncated": False,
    }

    page2 = build_view(
        stored, scope="live", query="", page=2, page_size=3, shop_domain="acme.myshopify.com"
    )

    assert page2["total"] == 7
    assert [i["title"] for i in page2["items"]] == ["P4", "P5", "P6"]
    assert page2["page"] == 2 and page2["page_size"] == 3
    past_end = build_view(
        stored, scope="live", query="", page=99, page_size=3, shop_domain="acme.myshopify.com"
    )
    assert past_end["items"] == [] and past_end["total"] == 7


def test_view_never_leaks_anything_beyond_the_documented_fields():
    stored = {
        "rows": [
            {
                "product_id": "gid://shopify/Product/1",
                "title": "T",
                "skus": ["S"],
                "status": "ACTIVE",
                "on_online_store": True,
                "stock": 1.0,
                "checkout_url": "https://secret",
            }
        ],
        "scanned": 1,
        "without_photos": 1,
        "live_without_photos": 1,
        "truncated": False,
    }

    (item,) = build_view(
        stored, scope="all", query="", page=1, page_size=10, shop_domain="acme.myshopify.com"
    )["items"]

    assert set(item) == {
        "product_id",
        "title",
        "skus",
        "status",
        "on_online_store",
        "stock",
        "admin_url",
    }
