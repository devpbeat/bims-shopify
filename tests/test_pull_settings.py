"""The pull and write-cycle settings: one minimum gap between pulls, the removed
cadence settings, and the staleness limit checked against the measured runs."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import sync, tenants
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import SyncMode, Tenant

ADMIN_TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
REMOVED = ("sync_interval_minutes", "push_interval_minutes", "reconcile_interval_minutes")


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

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        acme = await repo.create(
            Tenant(
                id=None,
                slug="acme",
                bims_base_url="https://bims.example.com",
                bims_api_key="k",
                shopify_shop_domain="acme.myshopify.com",
                shopify_access_token="t",
                shopify_webhook_secret="w",
                shopify_location_id="loc",
                bims_posale_id=1,
                bims_warehouse_id=1,
                bims_company_id=1,
                bims_currency_id=1,
                bims_payment_method_id=1,
                default_customer_contact_id=1,
            )
        )
    yield app, session_factory, acme
    await engine.dispose()
    os.unlink(db_file.name)



async def _record(session_factory, tenant_id: int, seconds: float, mode: str = "full") -> None:
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).record_run_duration(
            tenant_id, seconds, mode
        )



def _create_body(slug: str = "beta", **extra) -> dict:
    return {
        "slug": slug,
        "bims_base_url": "https://bims.example.com",
        "bims_api_key": "k",
        "shopify_shop_domain": f"{slug}.myshopify.com",
        "shopify_access_token": "t",
        "shopify_webhook_secret": "w",
        **extra,
    }


async def test_durations_are_recorded_per_mode_and_capped(ctx):
    _app, session_factory, acme = ctx
    for seconds in range(1, 31):
        await _record(session_factory, acme.id, float(seconds), "full")
    await _record(session_factory, acme.id, 999.0, "reconcile")

    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        full = await repo.get_recent_durations(acme.id, "full")
        cycles = await repo.get_recent_durations(acme.id, "reconcile")
    assert cycles == [999.0]  # a slow write cycle never evicts or pollutes the pull history
    assert len(full) == 10 and max(full) == 30.0 and min(full) == 21.0


async def test_the_minimum_gap_defaults_round_trips_and_is_live_editable(ctx):
    app, _sf, _acme = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        got = (await client.get("/tenants/acme", headers=HEADERS)).json()
        assert got["pull_min_gap_seconds"] == 60
        patched = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"pull_min_gap_seconds": 120}
        )
        assert patched.status_code == 200 and patched.json()["pull_min_gap_seconds"] == 120
        created = await client.post(
            "/tenants", headers=HEADERS, json=_create_body(pull_min_gap_seconds=45)
        )
        assert created.status_code == 201 and created.json()["pull_min_gap_seconds"] == 45


@pytest.mark.parametrize("gap", [0, 1, 9, 3601])
async def test_the_minimum_gap_is_range_checked_so_it_can_never_be_a_hot_loop(ctx, gap):
    app, _sf, _acme = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"pull_min_gap_seconds": gap}
        )
    assert response.status_code == 422


async def test_the_removed_settings_are_gone_from_the_api(ctx):
    app, _sf, _acme = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        tenant = (await client.get("/tenants/acme", headers=HEADERS)).json()
        listed = (await client.get("/tenants", headers=HEADERS)).json()[0]
        snapshot = (await client.get("/sync/acme/snapshot", headers=HEADERS)).json()
        status = (await client.get("/sync/acme/status", headers=HEADERS)).json()
    for name in REMOVED:
        assert name not in tenant and name not in listed
        assert name not in snapshot["settings"]
    assert snapshot["settings"]["pull_min_gap_seconds"] == 60
    for gone in ("sync_interval", "push_interval", "reconcile_interval"):
        assert gone not in status


@pytest.mark.parametrize("name", REMOVED)
async def test_sending_a_removed_setting_is_refused_not_silently_ignored(ctx, name):
    app, _sf, _acme = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        patch = await client.patch("/tenants/acme", headers=HEADERS, json={name: 15})
        post = await client.post("/tenants", headers=HEADERS, json=_create_body(**{name: 15}))
        put = await client.put("/tenants/acme", headers=HEADERS, json=_create_body("acme", **{name: 15}))
    for response in (patch, post, put):
        assert response.status_code == 422
        assert name in str(response.json()["detail"]) and "removed" in str(response.json()["detail"])


async def test_status_exposes_how_long_pulls_and_write_cycles_really_take(ctx):
    app, session_factory, acme = ctx
    await _record(session_factory, acme.id, 600.0, "full")
    await _record(session_factory, acme.id, 204.0, "reconcile")
    await _record(session_factory, acme.id, 240.0, "reconcile")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = (await client.get("/sync/acme/status", headers=HEADERS)).json()
    assert status["pull"]["min_gap_seconds"] == 60
    assert status["pull"]["slowest_recent_seconds"] == 600.0
    assert status["pull"]["sync_mode_setting"] == SyncMode.FULL.value
    assert status["write_cycle"]["slowest_recent_seconds"] == 240.0
    assert status["write_cycle"]["latest_seconds"] == 240.0
    assert status["write_cycle"]["runs_considered"] == 2


async def test_without_history_the_staleness_limit_is_not_second_guessed(ctx):
    app, _sf, _acme = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.patch("/tenants/acme", headers=HEADERS, json={"snapshot_max_age_minutes": 5})
    assert ok.status_code == 200


async def test_the_staleness_limit_must_tolerate_one_missed_pull_period(ctx):
    app, session_factory, acme = ctx
    await _record(session_factory, acme.id, 600.0, "full")  # a 10-minute pull + 60s gap = 11 min
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"snapshot_max_age_minutes": 20}
        )  # 20 < 2 x 11
        assert rejected.status_code == 422
        detail = rejected.json()["detail"]
        assert detail["field"] == "snapshot_max_age_minutes" and detail["min_minutes"] == 22

        accepted = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"snapshot_max_age_minutes": 22}
        )
        assert accepted.status_code == 200

        # Raising the gap alone can break the rule too: 600s + 600s = 20 min, 2 x 20 = 40 > 22.
        too_slow = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"pull_min_gap_seconds": 600}
        )
        assert too_slow.status_code == 422
        both = await client.patch(
            "/tenants/acme",
            headers=HEADERS,
            json={"pull_min_gap_seconds": 600, "snapshot_max_age_minutes": 40},
        )
        assert both.status_code == 200


async def test_the_staleness_limit_must_also_outlast_a_write_cycle(ctx):
    """A cycle starts when the snapshot lands and judges it again right before writing:
    a limit shorter than the cycle would make every cycle refuse its own snapshot."""
    app, session_factory, acme = ctx
    await _record(session_factory, acme.id, 1500.0, "reconcile")  # a 25-minute cycle
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"snapshot_max_age_minutes": 30}
        )
        accepted = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"snapshot_max_age_minutes": 50}
        )
    assert rejected.status_code == 422 and accepted.status_code == 200


async def test_an_unrelated_edit_is_not_blocked_by_a_pre_existing_low_limit(ctx):
    app, session_factory, acme = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.patch("/tenants/acme", headers=HEADERS, json={"snapshot_max_age_minutes": 5})
        await _record(session_factory, acme.id, 600.0, "full")
        other = await client.patch("/tenants/acme", headers=HEADERS, json={"auto_publish": False})
    assert other.status_code == 200
