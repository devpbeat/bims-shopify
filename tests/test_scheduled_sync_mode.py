"""Scheduled sync mode: every pull is a FULL BIMS comparison.

The incremental `last_update` filter proved unreliable on the BIMS side, and
since the pull/push split a partial pull cannot replace the persisted snapshot
(stock changes do not touch a product's `last_update`, so the unchanged rows
would keep stale stock). The pull therefore ignores `sync_mode`: a tenant
stored as `incremental` still pulls everything. The watermark itself is still
recorded after every successful write cycle.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import SyncMode, Tenant
from bims_shopify.main import _make_cycle_fn, _make_pull_fn
from tests.sync_world import inventory_response

async def _scheduled_cycle(session_factory, tenant: Tenant) -> None:
    """One pull followed by the write cycle its new snapshot triggers, as the scheduler runs them."""
    landed = await _make_pull_fn(session_factory)(tenant)
    assert landed is True
    await _make_cycle_fn(session_factory)(tenant)


GRAPHQL = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
BIMS_INDEX = "https://bims.example.com/api/products/index.json"
BIMS_STOCK = "https://bims.example.com/api/products_stocks/stock_fenicio.json"


@pytest.fixture
async def env():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="t",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    from alembic.config import Config

    from alembic import command

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = settings.database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    async def make_tenant(**overrides) -> Tenant:
        async with session_factory() as session:
            repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
            defaults = dict(
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
            )
            defaults.update(overrides)
            return await repo.create(Tenant(**defaults))

    yield session_factory, make_tenant
    await engine.dispose()
    os.unlink(db_file.name)


def _variants(*skus: str) -> dict:
    return {
        "data": {
            "productVariants": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": f"gid://shopify/ProductVariant/{i}",
                        "sku": sku,
                        "product": {"id": f"gid://shopify/Product/{i}", "title": sku},
                        "inventoryItem": {"id": f"gid://shopify/InventoryItem/{i}", "tracked": True},
                    }
                    for i, sku in enumerate(skus, start=1)
                ],
            }
        }
    }


def _bims_product(i: int, sku: str) -> dict:
    return {
        "Product": {
            "id": i,
            "name": sku,
            "code2": sku,
            "sell_price": "1000",
            "enabled": True,
            "exclude_ecommerce": False,
        }
    }


def _mock_world(stocks: dict[str, float], levels: dict[str, int] | None = None):
    """BIMS returns one product per SKU with the given stock; Shopify has a variant for each,
    holding ``levels`` (default: no inventory level yet). Returns (respx mock, list of
    inventorySetQuantities calls)."""
    pushes: list[str] = []
    skus = list(stocks)

    def graphql(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "inventorySetQuantities" in body:
            pushes.append(body)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "inventorySetQuantities": {
                            "inventoryAdjustmentGroup": {"createdAt": "2026-01-01T00:00:00Z"},
                            "userErrors": [],
                        }
                    }
                },
            )
        if "ListVariantInventory" in body:
            return httpx.Response(200, json=inventory_response(skus, dict(levels or {})))
        return httpx.Response(200, json=_variants(*skus))

    mock = respx.mock(assert_all_called=False)
    mock.get(BIMS_INDEX).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "count": str(len(skus)),
                "data": [_bims_product(i, s) for i, s in enumerate(skus, start=1)],
            },
        )
    )
    mock.get("https://bims.example.com/api/products/deleted.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )
    mock.post(BIMS_STOCK).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "data": {"stockPorSku": [{"sku": s, "stock": q} for s, q in stocks.items()]},
            },
        )
    )
    mock.post(GRAPHQL).mock(side_effect=graphql)
    return mock, pushes


def _index_requests(mock) -> list[httpx.Request]:
    return [c.request for c in mock.calls if str(c.request.url).startswith(BIMS_INDEX)]


async def _prime_watermark(session_factory, tenant: Tenant) -> datetime:
    when = datetime.now(UTC) - timedelta(hours=2)
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_last_run(tenant.id, when)
    return when


async def test_new_tenant_defaults_to_full(env):
    _session_factory, make_tenant = env
    tenant = await make_tenant()
    assert tenant.sync_mode is SyncMode.FULL


async def test_scheduled_sync_full_sends_no_last_update_but_records_watermark(env):
    session_factory, make_tenant = env
    tenant = await make_tenant()
    old = await _prime_watermark(session_factory, tenant)
    mock, _pushes = _mock_world({"SKU1": 5})

    with mock:
        await _scheduled_cycle(session_factory, tenant)
        requests = _index_requests(mock)
    assert requests, "BIMS catalog was never pulled"
    assert all("last_update" not in r.url.params for r in requests)

    async with session_factory() as session:
        status = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    assert datetime.fromisoformat(status["last_run_at"]) > old  # watermark still recorded
    assert status["last_run_summary"]["mode"] == "reconcile"


async def test_scheduled_sync_ignores_the_legacy_incremental_setting(env):
    session_factory, make_tenant = env
    tenant = await make_tenant(sync_mode=SyncMode.INCREMENTAL)
    await _prime_watermark(session_factory, tenant)
    mock, _pushes = _mock_world({"SKU1": 5})

    with mock:
        await _scheduled_cycle(session_factory, tenant)
        requests = _index_requests(mock)
    assert requests
    assert all("last_update" not in r.url.params for r in requests)  # still a full pull

    async with session_factory() as session:
        status = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    assert status["last_run_summary"]["mode"] == "reconcile"


async def test_scheduled_full_sync_guard_still_blocks_mass_zero(env):
    session_factory, make_tenant = env
    tenant = await make_tenant()
    skus = [f"SKU{i}" for i in range(10)]
    mock, pushes = _mock_world(
        {sku: 0 for sku in skus}, levels={sku: 5 for sku in skus}
    )  # BIMS suddenly says 0 everywhere, Shopify still holds 5

    with mock:
        await _scheduled_cycle(session_factory, tenant)

    assert pushes == []  # nothing reached Shopify
    async with session_factory() as session:
        entry = await SqlAlchemyAuditLogger(session).get_latest(
            action="sync.reconcile_needs_confirmation", tenant_id=tenant.id
        )
    assert entry is not None
    assert entry.payload["reason"] == "zero_ratio_exceeded"
    assert entry.payload["zero_count"] == 10 and entry.payload["drift"]["count"] == 10


async def test_needs_confirmation_result_reports_mode(env):
    from bims_shopify.api.sync import _do_run_sync

    session_factory, make_tenant = env
    tenant = await make_tenant()
    skus = [f"SKU{i}" for i in range(10)]
    mock, _pushes = _mock_world({sku: 0 for sku in skus}, levels={sku: 5 for sku in skus})
    with mock:
        async with session_factory() as session:
            result = await _do_run_sync(tenant, False, session, full=True)
    assert result["status"] == "needs_confirmation"
    assert result["mode"] == "reconcile" and result["report"]["guard"]["fired"] is True


async def test_manual_run_result_is_always_a_full_comparison(env):
    from bims_shopify.api.sync import _do_run_sync

    session_factory, make_tenant = env
    tenant = await make_tenant()
    await _prime_watermark(session_factory, tenant)
    mock, _pushes = _mock_world({"SKU1": 5})
    with mock:
        async with session_factory() as session:
            incremental = await _do_run_sync(tenant, False, session, full=False)
        async with session_factory() as session:
            full = await _do_run_sync(tenant, False, session, full=True)
        async with session_factory() as session:
            dry = await _do_run_sync(tenant, True, session, full=True)
    # `full=False` used to narrow the pull by the watermark; the flag is now
    # accepted but every run compares the whole catalog.
    assert incremental["mode"] == "reconcile"
    assert incremental["scanned"] == {"snapshot": 1, "shopify": 1}
    assert full["mode"] == "reconcile"
    assert dry["mode"] == "reconcile" and dry["dry_run"] is True


async def test_completed_runs_record_pull_and_push_durations_separately(env):
    session_factory, make_tenant = env
    tenant = await make_tenant()
    mock, _pushes = _mock_world({"SKU1": 5})
    with mock:
        await _scheduled_cycle(session_factory, tenant)  # full by default
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        pulls = await repo.get_recent_durations(tenant.id, "full")
        pushes = await repo.get_recent_durations(tenant.id, "reconcile")
        incremental = await repo.get_recent_durations(tenant.id, "incremental")
    assert len(pulls) == 1 and pulls[0] > 0
    assert len(pushes) == 1 and pushes[0] > 0
    assert incremental == []


async def test_scheduled_workers_swallow_a_tenant_failure_and_record_it(env):
    """One tenant's BIMS outage must not break the scheduler loop, but must be visible."""
    session_factory, make_tenant = env
    tenant = await make_tenant()
    with respx.mock(assert_all_called=False) as down:
        down.get(BIMS_INDEX).mock(return_value=httpx.Response(500))
        await _make_pull_fn(session_factory)(tenant)  # does not raise
    async with session_factory() as session:
        status = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    assert status["last_pull_outcome"]["status"] == "failed"
    assert status["last_error"].startswith("pull: ")

    # The write cycle needs no BIMS at all: with no snapshot it declines, it does not crash.
    await _make_cycle_fn(session_factory)(tenant)
    async with session_factory() as session:
        status = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    assert status["last_reconcile_outcome"]["reason"] == "missing"


async def test_scheduled_pull_then_write_cycle_chain_works_without_the_pull_waiting_on_the_cycle(env):
    session_factory, make_tenant = env
    tenant = await make_tenant()
    mock, pushes = _mock_world({"SKU1": 5})
    with mock:
        await _make_pull_fn(session_factory)(tenant)
        assert pushes == []  # the pull alone never writes to Shopify
        await _make_cycle_fn(session_factory)(tenant)
    assert len(pushes) == 1
