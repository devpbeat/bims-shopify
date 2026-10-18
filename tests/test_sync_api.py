"""Integration tests for POST /sync/{slug}/run and GET /sync/{slug}/status.

Covers: admin auth requirement, dry_run defaulting to True, the per-run
safety guard surfacing as needs_confirmation via HTTP, and status readback.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import health, payments, sync, tenants, webhooks
from tests.sync_world import inventory_response
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
async def app_and_tenant():
    # A real (temp) file-backed sqlite DB is required here rather than
    # ":memory:": Alembic's migration runner opens its own separate engine
    # (see env.py), and a second connection to "sqlite+aiosqlite:///:memory:"
    # gets its own independent, empty in-memory database rather than sharing
    # state with this fixture's `engine`. A temp file is visible to both.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Run real Alembic migrations (instead of Base.metadata.create_all) so
    # tests exercise the exact same schema-creation path as production and
    # catch drift between models.py and the migration scripts.
    from alembic.config import Config

    from alembic import command

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = settings.database_url
    try:
        # alembic's command API drives env.py, which calls asyncio.run()
        # internally; that fails if invoked directly inside this already-
        # running event loop, so run it in a worker thread instead.
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.include_router(health.router)
    app.include_router(tenants.router)
    app.include_router(webhooks.router)
    app.include_router(payments.router)
    app.include_router(sync.router)

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = Tenant(
            id=None,
            slug="acme",
            bims_base_url="https://bims.example.com",
            bims_api_key="acme_secretkey",
            shopify_shop_domain="acme.myshopify.com",
            shopify_access_token="shpat_test",
            shopify_webhook_secret="whsecret",
            shopify_location_id="gid://shopify/Location/1",
            bims_posale_id=1,
            bims_warehouse_id=1,
            bims_company_id=1,
            bims_currency_id=1,
            bims_payment_method_id=1,
            default_customer_contact_id=1,
            reorder_threshold=0.0,
            reorder_strategy=ReorderStrategy.NONE,
            payment_provider=PaymentProvider.BANCARD,
            provider_config={},
            field_mappings={},
            active=True,
        )
        await repo.create(tenant)

    yield app
    await engine.dispose()
    os.unlink(db_file.name)


def _empty_list_all_variants_response():
    return httpx.Response(
        200,
        json={
            "data": {
                "productVariants": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                }
            }
        },
    )


@pytest.fixture
def mock_bims_and_shopify_empty():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://bims.example.com/api/products/index.json").mock(
            return_value=httpx.Response(200, json={"status": "ok", "count": "0", "data": []})
        )
        mock.post("https://bims.example.com/api/products_stocks/stock_fenicio.json").mock(
            return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": []}})
        )
        mock.post("https://acme.myshopify.com/admin/api/2025-07/graphql.json/").mock(
            return_value=_empty_list_all_variants_response()
        )
        yield mock


async def test_run_sync_requires_admin_auth(app_and_tenant):
    transport = ASGITransport(app=app_and_tenant)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/sync/acme/run")
    assert response.status_code == 401


async def test_run_sync_dry_run_defaults_to_true(app_and_tenant, mock_bims_and_shopify_empty):
    transport = ASGITransport(app=app_and_tenant)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/sync/acme/run", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True


async def test_run_sync_status_endpoint_reports_last_run(app_and_tenant, mock_bims_and_shopify_empty):
    transport = ASGITransport(app=app_and_tenant)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/sync/acme/run",
            params={"dry_run": "false"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        status_response = await client.get(
            "/sync/acme/status", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["last_run_summary"]["status"] == "ok"
    assert body["last_run_at"] is not None

def _graphql_url() -> str:
    return "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"


@pytest.fixture
def shopify_levels() -> dict[str, int]:
    """What the fake Shopify really holds per SKU (absent: no inventory level)."""
    return {}


@pytest.fixture
def mock_bims_one_product_and_shopify_variant(shopify_levels):
    """BIMS returns one product with stock=5; Shopify holds ``shopify_levels`` for it and
    applies the inventory writes it receives. Used to prove a run compares with what Shopify
    really holds."""
    list_all_variants_response = {
        "data": {
            "productVariants": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": "gid://shopify/ProductVariant/1",
                        "sku": "SKU1",
                        "product": {"id": "gid://shopify/Product/1", "title": "A"},
                        "inventoryItem": {"id": "gid://shopify/InventoryItem/1", "tracked": True},
                    }
                ],
            }
        }
    }
    set_quantities_response = {
        "data": {
            "inventorySetQuantities": {
                "inventoryAdjustmentGroup": {"createdAt": "2026-01-01T00:00:00Z"},
                "userErrors": [],
            }
        }
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://bims.example.com/api/products/index.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "ok",
                    "count": "1",
                    "data": [
                        {
                            "Product": {
                                "id": 1,
                                "name": "A",
                                "code2": "SKU1",
                                "sell_price": "1000",
                                "enabled": True,
                                "exclude_ecommerce": False,
                            }
                        }
                    ],
                },
            )
        )
        mock.post("https://bims.example.com/api/products_stocks/stock_fenicio.json").mock(
            return_value=httpx.Response(
                200,
                json={"status": "OK", "data": {"stockPorSku": [{"sku": "SKU1", "stock": 5}]}},
            )
        )
        # Answer by operation, not by call order: a sync lists Shopify variants
        # more than once (the pull's catalog stats, the push's SKU map) and
        # writes only when something changed.
        def graphql(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "inventorySetQuantities" in body["query"]:
                for q in body["variables"]["input"]["quantities"]:
                    shopify_levels["SKU1"] = q["quantity"]
                return httpx.Response(200, json=set_quantities_response)
            if "ListVariantInventory" in body["query"]:
                return httpx.Response(200, json=inventory_response(["SKU1"], dict(shopify_levels)))
            return httpx.Response(200, json=list_all_variants_response)

        mock.post(_graphql_url()).mock(side_effect=graphql)
        yield mock


async def test_a_run_corrects_whatever_shopify_holds_no_matter_what_was_sent_before(
    app_and_tenant, mock_bims_one_product_and_shopify_variant, shopify_levels
):
    """Regression for the old frozen-SKU bug (a stored "last sent" value that matched BIMS
    while Shopify had drifted after a wipe+rebuild): the run compares with what Shopify
    really holds, so there is nothing stored that can make it report "already in sync"."""
    transport = ASGITransport(app=app_and_tenant)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        shopify_levels["SKU1"] = 1  # Shopify drifted; BIMS says 5
        drifted = await client.post(
            "/sync/acme/run",
            params={"dry_run": "false"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert drifted.status_code == 200
        body = drifted.json()
        assert body["status"] == "ok" and body["updated"] == 1
        assert body["drift"]["count"] == 1
        assert body["scanned"] == {"snapshot": 1, "shopify": 1}
        assert shopify_levels["SKU1"] == 5

        settled = await client.post(
            "/sync/acme/run",
            params={"dry_run": "false"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert settled.json()["drift"]["count"] == 0 and settled.json()["updated"] == 0

        shopify_levels["SKU1"] = 9
        forced = await client.post(
            "/sync/acme/run",
            params={"dry_run": "false", "force": "true"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert forced.status_code == 200
    assert forced.json()["report"]["guard"]["forced"] is True
    assert shopify_levels["SKU1"] == 5


async def test_get_reconciliation_404s_when_none_persisted(app_and_tenant):
    transport = ASGITransport(app=app_and_tenant)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/sync/acme/reconciliation", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
    assert response.status_code == 404

async def test_get_reconciliation_returns_latest_persisted_entry(app_and_tenant):
    from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
    from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository

    async with app_and_tenant.state.session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(app_and_tenant.state.settings.fernet_key))
        tenant = await repo.get_by_slug("acme")
        audit = SqlAlchemyAuditLogger(session)
        await audit.log(
            actor="system",
            action="catalog.reconciliation",
            entity="catalog",
            tenant_id=tenant.id,
            payload={"bims": {"eligible_products": 1}, "reconciliation": {"matched_skus": 0}},
        )
        # A second, newer entry must win over the first.
        await audit.log(
            actor="system",
            action="catalog.reconciliation",
            entity="catalog",
            tenant_id=tenant.id,
            payload={"bims": {"eligible_products": 2}, "reconciliation": {"matched_skus": 1}},
        )

    transport = ASGITransport(app=app_and_tenant)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/sync/acme/reconciliation", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["payload"]["bims"]["eligible_products"] == 2
    assert body["created_at"] is not None

async def test_get_reconciliation_requires_admin_auth(app_and_tenant):
    transport = ASGITransport(app=app_and_tenant)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/sync/acme/reconciliation")
    assert response.status_code == 401


async def test_sync_summary_persists_real_phase_timings(
    app_and_tenant, mock_bims_one_product_and_shopify_variant
):
    """Per-phase durations are persisted (not just logged) so a minimum safe
    scheduler interval can be derived from them later."""
    transport = ASGITransport(app=app_and_tenant)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run = await client.post(
            "/sync/acme/run",
            params={"dry_run": "false", "force": "true"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        status = await client.get(
            "/sync/acme/status", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
    summary = status.json()["last_run_summary"]
    for timings in (run.json()["timings"], summary["timings"]):
        # The pull's phase durations travel with the snapshot the write read.
        assert timings["catalog_pull_seconds"] > 0
        assert timings["stock_fetch_seconds"] > 0
        assert 0 < timings["reconcile_seconds"] <= summary["duration_seconds"]
    assert summary["catalog_pull"]["rows"] == 1
    assert summary["catalog_pull"]["complete"] is True
