"""Reading the snapshot metadata: GET /sync/{slug}/snapshot and the dashboard.

The metadata is how an operator tells "the pull is healthy" from "the pull has
been rejected for two days": when the current snapshot was finished, how many
rows it holds, whether BIMS said it was complete, how long each phase took,
and what the last pull and write cycle did.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import portal, sync, tenants
from bims_shopify.config import Settings
from bims_shopify.domain.snapshot import CatalogPull, CatalogRow
from bims_shopify.domain.tenant import Tenant

ADMIN_TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
async def ctx():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token=ADMIN_TOKEN,
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

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.include_router(tenants.router)
    app.include_router(sync.router)
    app.include_router(portal.router)

    async with session_factory() as session:
        acme = await SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key)).create(
            Tenant(
                id=None, slug="acme", bims_base_url="https://bims.example.com", bims_api_key="k",
                shopify_shop_domain="acme.myshopify.com", shopify_access_token="t",
                shopify_webhook_secret="w", shopify_location_id="loc", bims_posale_id=1,
                bims_warehouse_id=1, bims_company_id=1, bims_currency_id=1,
                bims_payment_method_id=1, default_customer_contact_id=1,
            )
        )
    yield app, session_factory, acme
    await engine.dispose()
    os.unlink(db_file.name)


async def _land(session_factory, tenant_id: int, n: int, *, finished_ago: timedelta, stats=None):
    rows = [CatalogRow(str(i), f"S{i}", f"S{i}", f"P{i}", 1.0, True, False, 1) for i in range(n)]
    pull = CatalogPull(
        rows=rows, stock={r.sku: 1.0 for r in rows}, reported_count=n, complete=True, pages=2,
        timings={"catalog_pull_seconds": 36.7, "stock_fetch_seconds": 59.4},
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        staged = await repo.stage(tenant_id, pull, now - finished_ago - timedelta(seconds=100))
        return await repo.promote(staged.id, finished_at=now - finished_ago, stats=stats)


async def _get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=HEADERS)


async def test_snapshot_endpoint_without_a_snapshot_says_so(ctx):
    app, _sf, _acme = ctx
    response = await _get(app, "/sync/acme/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert body["current"] is None and body["history"] == []
    assert body["last_pull"] is None and body["last_reconcile"] is None
    assert "last_push" not in body


async def test_snapshot_endpoint_unknown_tenant_is_404(ctx):
    app, _sf, _acme = ctx
    assert (await _get(app, "/sync/nope/snapshot")).status_code == 404


async def test_snapshot_endpoint_exposes_metadata_age_history_and_worker_outcomes(ctx):
    app, session_factory, acme = ctx
    await _land(session_factory, acme.id, 3, finished_ago=timedelta(minutes=50))
    await _land(session_factory, acme.id, 5, finished_ago=timedelta(minutes=10),
                stats={"bims": {"eligible_products": 5}})
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        await repo.set_last_pull_outcome(acme.id, {"status": "ok", "at": "2026-10-15T10:00:00+00:00"})
        await repo.set_last_reconcile_outcome(
            acme.id, {"status": "ok", "drift": {"count": 4, "total_abs_delta": 9, "sample": []}}
        )

    body = (await _get(app, "/sync/acme/snapshot")).json()

    current = body["current"]
    assert current["status"] == "current"
    assert current["rows_pulled"] == 5 and current["stock_rows"] == 5
    assert current["complete"] is True and current["reported_count"] == 5
    assert current["timings"]["catalog_pull_seconds"] == 36.7
    assert 9.5 <= current["age_minutes"] <= 11
    assert current["stats"] == {"bims": {"eligible_products": 5}}
    assert body["max_age_minutes"] == 120 and body["usable_for_write"] is True
    assert [h["rows_pulled"] for h in body["history"]] == [5, 3]
    assert [h["status"] for h in body["history"]] == ["current", "superseded"]
    assert body["last_pull"]["status"] == "ok"
    assert "last_push" not in body
    assert body["last_reconcile"]["status"] == "ok"
    assert body["last_reconcile"]["drift"]["count"] == 4
    assert body["settings"] == {
        "pull_min_gap_seconds": 60,
        "snapshot_max_age_minutes": 120,
        "pull_max_drop_ratio": 0.3,
        "auto_create_missing": True,
        "auto_publish": True,
    }


async def test_snapshot_endpoint_flags_a_snapshot_the_write_cycle_would_decline(ctx):
    app, session_factory, acme = ctx
    await _land(session_factory, acme.id, 3, finished_ago=timedelta(hours=5))
    body = (await _get(app, "/sync/acme/snapshot")).json()
    assert body["usable_for_write"] is False
    assert body["decline"]["reason"] == "stale"


async def test_sync_status_carries_the_pull_outcome_and_no_push_outcome(ctx):
    app, session_factory, acme = ctx
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_last_pull_outcome(
            acme.id, {"status": "rejected", "reason": "too_few_rows"}
        )
    body = (await _get(app, "/sync/acme/status")).json()
    assert body["last_pull_outcome"]["reason"] == "too_few_rows"
    assert "last_push_outcome" not in body


async def test_dashboard_shows_the_snapshot_and_the_worker_outcomes(ctx):
    app, session_factory, acme = ctx
    await _land(session_factory, acme.id, 4, finished_ago=timedelta(minutes=20))
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_last_pull_outcome(
            acme.id, {"status": "ok", "at": "2026-10-15T10:00:00+00:00"}
        )
    token = (
        await _post_token(app)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (
            await client.get("/api/portal/acme/dashboard", headers={"Authorization": f"Bearer {token}"})
        ).json()
    assert body["snapshot"]["rows"] == 4
    assert body["snapshot"]["complete"] is True
    assert 19 <= body["snapshot"]["age_minutes"] <= 21
    assert body["workers"]["pull"]["status"] == "ok"
    assert body["workers"]["write"] is None and "push" not in body["workers"]


async def test_dashboard_hint_points_at_the_pull_not_at_a_manual_status_run(ctx):
    app, _sf, _acme = ctx
    token = await _post_token(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (
            await client.get("/api/portal/acme/dashboard", headers={"Authorization": f"Bearer {token}"})
        ).json()
    assert body["snapshot"] is None
    assert "pull" in body["catalog"]["hint"].lower()
    assert "Run Status" not in body["catalog"]["hint"]


async def _post_token(app) -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/tenants/acme/portal-token", headers=HEADERS)
    return response.json()["portal_token"]


async def test_snapshot_endpoint_has_no_reconcile_outcome_before_the_first_one(ctx):
    app, _session_factory, _acme = ctx
    body = (await _get(app, "/sync/acme/snapshot")).json()
    assert body["last_reconcile"] is None


async def test_sync_status_carries_the_write_outcome_and_its_skips(ctx):
    app, session_factory, acme = ctx
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_last_reconcile_outcome(
            acme.id,
            {
                "status": "needs_confirmation",
                "drift": {"count": 800},
                "last_skip": {"status": "skipped", "skipped_total": 3},
            },
        )
    body = (await _get(app, "/sync/acme/status")).json()
    assert body["last_reconcile_outcome"]["drift"]["count"] == 800
    assert body["last_reconcile_outcome"]["last_skip"]["skipped_total"] == 3


async def test_snapshot_endpoint_reports_what_each_self_healing_stage_did(ctx):
    app, session_factory, acme = ctx
    body = (await _get(app, "/sync/acme/snapshot")).json()
    assert body["last_heal"] is None
    assert body["settings"]["auto_create_missing"] is True
    assert body["settings"]["auto_publish"] is True

    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_last_heal_outcome(
            acme.id,
            {
                "create": {"status": "ok", "created": {"products": 2, "variants": 6, "sample": []}},
                "publish": {"status": "ok", "published": {"count": 2, "sample": []}},
            },
        )
    body = (await _get(app, "/sync/acme/snapshot")).json()
    assert body["last_heal"]["create"]["created"]["products"] == 2
    assert body["last_heal"]["publish"]["published"]["count"] == 2

    status = (await _get(app, "/sync/acme/status")).json()
    assert status["last_heal_outcome"]["create"]["status"] == "ok"
