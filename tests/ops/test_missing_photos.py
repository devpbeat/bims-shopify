"""The ``missing_photos`` ops command: a read-only Shopify scan joined with the stored stock."""
from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet

from bims_shopify.config import Settings
from bims_shopify.ops import catalog as catalog_ops
from bims_shopify.ops import job_runner, missing_photos
from tests.sync_world import land_snapshot

ONLINE_STORE = "gid://shopify/Publication/1"


def _product(n: int, *, media: int = 0, status: str = "ACTIVE", sku: str | None = None):
    return {
        "id": f"gid://shopify/Product/{n}",
        "title": f"Product {n}",
        "status": status,
        "skus": [sku or f"SKU-{n}"],
        "media_count": media,
        "published_publication_ids": [ONLINE_STORE],
    }


def _install(monkeypatch, session_factory, products, *, publications_error=None, count_error=None):
    url = session_factory.kw["bind"].url.render_as_string(hide_password=False)
    settings = Settings(
        admin_token="t", fernet_key=Fernet.generate_key().decode("utf-8"), database_url=url
    )
    monkeypatch.setattr(catalog_ops, "get_settings", lambda: settings)
    calls: list[str] = []

    class _FakeShopify:
        """Read-only surface only: any mutation attempt raises AttributeError."""

        def __init__(self, tenant):
            pass

        async def iter_all_products_with_skus(self):
            calls.append("iter")
            for product in products:
                yield product

        async def fetch_publications(self):
            if publications_error is not None:
                raise publications_error
            return [{"id": ONLINE_STORE, "name": "Online Store"}]

        async def products_count(self):
            if count_error is not None:
                raise count_error
            return len(products)

        async def aclose(self):
            calls.append("aclose")

    monkeypatch.setattr(missing_photos, "ShopifyClient", _FakeShopify)
    return calls


async def test_scan_returns_photoless_products_with_the_stored_stock(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"SKU-1": 6.0, "SKU-2": 2.0})
    calls = _install(
        monkeypatch,
        session_factory,
        [_product(1), _product(2, media=2), _product(3, sku="SKU-3", status="DRAFT")],
    )

    result = await missing_photos.run_missing_photos(tenant, {})

    assert [r["skus"] for r in result["rows"]] == [["SKU-1"], ["SKU-3"]]
    assert [r["stock"] for r in result["rows"]] == [6.0, None]  # SKU-3 is unknown to the snapshot
    assert result["scanned"] == 3
    assert result["without_photos"] == 2
    assert result["live_without_photos"] == 1
    assert result["stock_as_of"] is not None
    assert calls[-1] == "aclose"


async def test_scan_without_a_snapshot_reports_unknown_stock_not_zero(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    _install(monkeypatch, session_factory, [_product(1)])

    result = await missing_photos.run_missing_photos(tenant, {})

    assert result["rows"][0]["stock"] is None
    assert result["stock_as_of"] is None


async def test_unresolvable_publication_does_not_fail_the_scan(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    _install(
        monkeypatch,
        session_factory,
        [_product(1)],
        publications_error=httpx.ConnectError("boom"),
    )

    result = await missing_photos.run_missing_photos(tenant, {})

    assert result["rows"][0]["on_online_store"] is None


async def test_unavailable_product_count_only_loses_the_progress_total(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    _install(monkeypatch, session_factory, [_product(1)], count_error=httpx.ConnectError("boom"))
    totals: list[int | None] = []

    async def progress(done, total, message):
        totals.append(total)

    result = await missing_photos.run_missing_photos(tenant, {}, progress)

    assert result["without_photos"] == 1
    assert totals and set(totals) == {None}


def test_the_job_runner_knows_the_command():
    assert job_runner._COMMAND_RUNNERS["missing_photos"] is missing_photos.run_missing_photos


@pytest.mark.parametrize("command", ["missing_photos"])
def test_the_ops_api_accepts_the_command(command):
    from bims_shopify.api import ops

    assert command in ops._VALID_COMMANDS
    assert ops.RunJobRequest(command=command).command == command
