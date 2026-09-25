"""Integration tests for running inventory sync as a background ops job.

A full/force sync can take minutes (33k BIMS products, ~10k SKUs of stock),
which used to run synchronously inside `POST /sync/{slug}/run` and made
Traefik time out with a 502. These tests cover the fix: `sync` as an ops
command that runs in-process via JobRunner (job_id returned instantly,
progress/result/error polled via GET /ops/{slug}/jobs/{job_id}), reusing the
exact same sync logic as the HTTP endpoint.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import OpsJobModel
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import health, ops
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant
from bims_shopify.ops.job_runner import JobRunner

ADMIN_TOKEN = "test-admin-token"


async def _run_migrations(database_url: str, repo_root: Path) -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)


@pytest.fixture
async def app_and_tenant(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    # sync_job.run_sync opens its own engine via get_settings() /
    # create_engine_and_sessionmaker(), separate from JobRunner's
    # session_factory (matching the existing catalog.py ops-job convention)
    # -- so it must resolve to this same temp DB.
    monkeypatch.setattr("bims_shopify.ops.sync_job.get_settings", lambda: settings)

    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    repo_root = Path(__file__).resolve().parents[1]
    await _run_migrations(settings.database_url, repo_root)

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    job_runner = JobRunner(session_factory, settings)
    app.state.job_runner = job_runner
    app.include_router(health.router)
    app.include_router(ops.router)

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
        created = await repo.create(tenant)

    yield app, created, session_factory, job_runner
    await engine.dispose()
    os.unlink(db_file.name)


async def _wait_for_status(
    session_factory, job_id: int, statuses: set[str], timeout: float = 5.0
) -> OpsJobModel:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with session_factory() as session:
            result = await session.execute(select(OpsJobModel).where(OpsJobModel.id == job_id))
            job = result.scalar_one()
            if job.status in statuses:
                return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {statuses}, last status={job.status}")


def _graphql_url() -> str:
    return "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"


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
        mock.post(_graphql_url()).mock(return_value=_empty_list_all_variants_response())
        yield mock


@pytest.fixture
def mock_bims_one_product_and_shopify_variant():
    """BIMS returns one product with stock=5; Shopify resolves its variant
    and accepts the inventory push."""
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
        mock.post(_graphql_url()).mock(
            side_effect=[
                httpx.Response(200, json=list_all_variants_response),
                httpx.Response(200, json=set_quantities_response),
            ]
        )
        yield mock


async def test_sync_job_force_pushes_end_to_end(
    app_and_tenant, mock_bims_one_product_and_shopify_variant
):
    app, tenant, session_factory, _job_runner = app_and_tenant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/acme/run",
            json={"command": "sync", "options": {"dry_run": False, "force": True}},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]

        job = await _wait_for_status(session_factory, job_id, {"succeeded", "failed"})

    assert job.status == "succeeded", job.error
    assert job.result == {"status": "ok", "synced_deltas": 1}

    # Same persistence as the HTTP path: last_run + product_hashes advance.
    from bims_shopify.adapters.persistence.sync_state_repository import (
        SqlAlchemySyncStateRepository,
    )

    async with session_factory() as session:
        sync_state_repo = SqlAlchemySyncStateRepository(session)
        status = await sync_state_repo.get_status(tenant.id)
        assert status["last_run_summary"]["status"] == "ok"
        assert status["last_run_summary"]["matched"] == 1


async def test_sync_job_dry_run_reports(app_and_tenant, mock_bims_and_shopify_empty):
    app, _tenant, session_factory, _job_runner = app_and_tenant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/acme/run",
            json={"command": "sync", "options": {"dry_run": True}},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]

        job = await _wait_for_status(session_factory, job_id, {"succeeded", "failed"})

    assert job.status == "succeeded", job.error
    assert job.result["dry_run"] is True
    assert job.result["report"]["total_products"] == 0


async def test_sync_job_default_options_are_dry_run_false(app_and_tenant, monkeypatch):
    """options default to dry_run=False, full=False, force=False."""
    app, _tenant, session_factory, _job_runner = app_and_tenant
    seen_options: list[dict] = []

    async def _capture_runner(tenant, options, progress):
        seen_options.append(options)
        return {"status": "ok", "synced_deltas": 0}

    monkeypatch.setitem(
        __import__("bims_shopify.ops.job_runner", fromlist=["_COMMAND_RUNNERS"])._COMMAND_RUNNERS,
        "sync",
        _capture_runner,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/acme/run",
            json={"command": "sync", "options": {}},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert response.status_code == 200
        await _wait_for_status(session_factory, response.json()["job_id"], {"succeeded", "failed"})

    assert seen_options == [{"dry_run": False, "full": False, "force": False}]


async def test_sync_options_bad_type_is_422(app_and_tenant):
    app, _tenant, _sf, _job_runner = app_and_tenant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/acme/run",
            json={"command": "sync", "options": {"dry_run": "yes"}},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert response.status_code == 422


async def test_sync_job_waits_for_shared_tenant_lock(app_and_tenant, monkeypatch):
    """The sync job must not run concurrently with the scheduler tick or a
    manual HTTP sync for the same tenant: it acquires the same shared
    per-tenant lock (simulated here via set_sync_lock_provider), not just
    its own (tenant, "sync") JobRunner lock."""
    app, _tenant, session_factory, job_runner = app_and_tenant

    shared_lock = asyncio.Lock()
    job_runner.set_sync_lock_provider(lambda _tenant_id: shared_lock)

    executed = asyncio.Event()

    async def _fake_sync_runner(tenant, options, progress):
        executed.set()
        return {"status": "ok", "synced_deltas": 0}

    monkeypatch.setitem(
        __import__("bims_shopify.ops.job_runner", fromlist=["_COMMAND_RUNNERS"])._COMMAND_RUNNERS,
        "sync",
        _fake_sync_runner,
    )

    # Simulate a scheduler tick / manual HTTP sync already holding the lock.
    await shared_lock.acquire()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/ops/acme/run",
                json={"command": "sync", "options": {}},
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
            assert response.status_code == 200
            job_id = response.json()["job_id"]

            await asyncio.sleep(0.1)
            assert not executed.is_set()

            async with session_factory() as session:
                result = await session.execute(
                    select(OpsJobModel).where(OpsJobModel.id == job_id)
                )
                job = result.scalar_one()
                assert job.status in ("queued", "running")
    finally:
        shared_lock.release()

    await asyncio.wait_for(executed.wait(), timeout=5.0)
    job = await _wait_for_status(session_factory, job_id, {"succeeded", "failed"})
    assert job.status == "succeeded"
