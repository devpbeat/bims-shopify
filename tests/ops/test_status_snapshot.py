"""`status` reads the BIMS side from the persisted snapshot when there is one.

It is a read-only report: a few-minutes-old snapshot answers it without making
BIMS pull the whole catalog three times per refresh. `live: true` (CLI
`--live`) forces the old behavior. Commands that gate WRITES on fresh stock
(`publish`) or explain a live discrepancy (`diagnose`) are NOT switched.
"""
from __future__ import annotations

import json

import httpx
import respx
from cryptography.fernet import Fernet

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.config import Settings
from bims_shopify.ops import catalog as catalog_ops
from tests.sync_world import BIMS_INDEX, BIMS_STOCK, GRAPHQL, land_snapshot


def _settings_for(session_factory) -> Settings:
    url = session_factory.kw["bind"].url.render_as_string(hide_password=False)
    return Settings(
        admin_token="t", fernet_key=Fernet.generate_key().decode("utf-8"), database_url=url
    )


def _mock_shopify(shopify_skus: list[str]):
    def graphql(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        nodes_page = {"pageInfo": {"hasNextPage": False, "endCursor": None}}
        if "productVariants" in query:
            nodes = [
                {"id": f"v{i}", "sku": s, "product": {"id": f"p{i}", "title": s}}
                for i, s in enumerate(shopify_skus)
            ]
            return httpx.Response(200, json={"data": {"productVariants": {**nodes_page, "nodes": nodes}}})
        nodes = [{"id": f"p{i}", "title": s} for i, s in enumerate(shopify_skus)]
        return httpx.Response(200, json={"data": {"products": {**nodes_page, "nodes": nodes}}})

    mock = respx.mock(assert_all_called=False)
    mock.post(GRAPHQL).mock(side_effect=graphql)
    bims_requests: list[str] = []
    mock.get(BIMS_INDEX).mock(
        side_effect=lambda r: bims_requests.append("index") or httpx.Response(200, json={"status": "ok", "count": "1", "data": [{"Product": {"id": 9, "name": "LIVE", "code2": "LIVE", "sell_price": "1", "enabled": True, "exclude_ecommerce": False}}]}) if int(r.url.params.get("offset", 0)) == 0 else httpx.Response(200, json={"status": "ok", "count": "1", "data": []})
    )
    mock.post(BIMS_STOCK).mock(
        side_effect=lambda r: bims_requests.append("stock") or httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": [{"sku": "LIVE", "stock": 7}]}})
    )
    return mock, bims_requests


async def _status(monkeypatch, session_factory, tenant, **options):
    monkeypatch.setattr(catalog_ops, "get_settings", lambda: _settings_for(session_factory))
    return await catalog_ops.run_status(tenant, options)


async def test_status_uses_the_snapshot_and_does_not_touch_bims(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 0, "C": 2})
    mock, bims_requests = _mock_shopify(["A", "ONLY-SHOPIFY"])
    with mock:
        report = await _status(monkeypatch, session_factory, tenant)

    assert bims_requests == []
    assert report["source"] == "snapshot"
    assert report["snapshot"]["id"] > 0 and report["snapshot"]["age_minutes"] >= 0
    assert report["bims"]["eligible_variants"] == 3
    assert report["bims"]["with_stock"] == 2
    assert report["shopify"] == {"products": 2, "variants": 2, "variants_with_sku": 2}
    assert report["reconciliation"]["matched_skus"] == 1
    assert report["catalog_pull"]["rows"] == 3
    assert report["timings"]["catalog_pull_seconds"] == 1.0
    async with session_factory() as session:
        entry = await SqlAlchemyAuditLogger(session).get_latest(
            action="catalog.reconciliation", tenant_id=tenant.id
        )
    assert entry.payload["source"] == "snapshot"


async def test_status_live_option_forces_a_fresh_bims_pull(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    mock, bims_requests = _mock_shopify(["LIVE"])
    with mock:
        report = await _status(monkeypatch, session_factory, tenant, live=True)

    assert "index" in bims_requests and "stock" in bims_requests
    assert report["source"] == "live"
    assert report["bims"]["with_stock"] == 1 and report["reconciliation"]["matched_skus"] == 1


async def test_status_without_a_snapshot_falls_back_to_the_live_pull(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    mock, bims_requests = _mock_shopify(["LIVE"])
    with mock:
        report = await _status(monkeypatch, session_factory, tenant)
    assert report["source"] == "live" and "index" in bims_requests


async def test_status_numbers_agree_between_the_snapshot_and_the_live_path(migrated_db, monkeypatch):
    """Both paths go through the same bims_catalog_stats, so they cannot drift."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"LIVE": 7})
    mock, _ = _mock_shopify(["LIVE"])
    with mock:
        from_snapshot = await _status(monkeypatch, session_factory, tenant)
        live = await _status(monkeypatch, session_factory, tenant, live=True)
    assert from_snapshot["bims"] == live["bims"]
    assert from_snapshot["reconciliation"] == live["reconciliation"]
