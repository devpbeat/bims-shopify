"""Pure rules for the "products without photos" view: liveness, filtering, admin link."""
from __future__ import annotations

from bims_shopify.domain.products_without_photos import (
    SCOPE_ALL,
    SCOPE_LIVE,
    ProductWithoutPhotos,
    admin_product_url,
    filter_rows,
    is_live,
)


def _row(
    n: int = 1,
    *,
    status: str | None = "ACTIVE",
    on_online_store: bool | None = True,
    skus: tuple[str, ...] = ("SKU-1",),
    title: str | None = "Blue Shirt",
    stock: float | None = 3.0,
) -> ProductWithoutPhotos:
    return ProductWithoutPhotos(
        product_id=f"gid://shopify/Product/{n}",
        title=title,
        skus=skus,
        status=status,
        on_online_store=on_online_store,
        stock=stock,
    )


def test_active_product_on_the_online_store_is_live():
    assert is_live(_row()) is True


def test_draft_product_is_not_live():
    assert is_live(_row(status="DRAFT")) is False


def test_active_product_known_to_be_off_the_online_store_is_not_live():
    assert is_live(_row(on_online_store=False)) is False


def test_active_product_with_unknown_publication_is_still_treated_as_live():
    """Unknown is not a reason to hide a product the customers may be seeing."""
    assert is_live(_row(on_online_store=None)) is True


def test_scope_live_keeps_only_live_rows_and_scope_all_keeps_everything():
    rows = [_row(1), _row(2, status="DRAFT"), _row(3, on_online_store=False)]

    assert [r.product_id[-1] for r in filter_rows(rows, SCOPE_LIVE, "")] == ["1"]
    assert len(filter_rows(rows, SCOPE_ALL, "")) == 3


def test_query_matches_title_or_sku_case_insensitively():
    rows = [
        _row(1, title="Blue Shirt", skus=("AAA-1",)),
        _row(2, title="Red Hat", skus=("zzz-9",)),
        _row(3, title=None, skus=()),
    ]

    assert [r.product_id[-1] for r in filter_rows(rows, SCOPE_ALL, "shirt")] == ["1"]
    assert [r.product_id[-1] for r in filter_rows(rows, SCOPE_ALL, " ZZZ ")] == ["2"]
    assert filter_rows(rows, SCOPE_ALL, "nothing") == []


def test_admin_url_points_at_the_product_page_of_the_shop():
    url = admin_product_url("acme.myshopify.com", "gid://shopify/Product/8765")

    assert url == "https://acme.myshopify.com/admin/products/8765"


def test_admin_url_is_none_when_the_id_is_not_a_product_gid():
    assert admin_product_url("acme.myshopify.com", "not-a-gid") is None
    assert admin_product_url("acme.myshopify.com", "gid://shopify/Product/") is None


def test_row_round_trips_through_its_stored_dict_form():
    row = _row(5, skus=("A", "B"), stock=None)

    assert ProductWithoutPhotos.from_dict(row.to_dict()) == row
