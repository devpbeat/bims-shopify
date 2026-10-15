"""Measured minimum safe sync interval.

The system records how long its own syncs take and derives a floor from the
slowest recent run; the tenant API rejects (422) any interval below it.
"""
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
from bims_shopify.application.sync_interval import (
    ABSOLUTE_MIN_INTERVAL_MINUTES,
    DEFAULT_MIN_INTERVAL_MINUTES,
    SAFETY_FACTOR,
    compute_interval_floor,
)
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import SyncMode, Tenant

ADMIN_TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# -- pure floor computation ------------------------------------------------


def test_no_history_falls_back_to_the_documented_conservative_default():
    floor = compute_interval_floor([])
    assert floor.basis == "default"
    assert floor.minutes == DEFAULT_MIN_INTERVAL_MINUTES == 30
    assert floor.slowest_seconds is None
    assert floor.runs_considered == 0


def test_floor_is_slowest_recent_run_times_safety_factor_rounded_up():
    # 204s is the measured production full sync (3.4 minutes).
    floor = compute_interval_floor([180.0, 204.0, 190.0])
    assert SAFETY_FACTOR == 3
    assert floor.basis == "measured"
    assert floor.slowest_seconds == 204.0
    assert floor.runs_considered == 3
    assert floor.minutes == 11  # ceil(204 * 3 / 60 = 10.2)


def test_floor_never_drops_below_the_absolute_minimum():
    assert compute_interval_floor([4.0]).minutes == ABSOLUTE_MIN_INTERVAL_MINUTES


# -- persistence -----------------------------------------------------------


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


async def test_durations_are_recorded_per_mode_and_capped(ctx):
    _app, session_factory, acme = ctx
    for seconds in range(1, 31):
        await _record(session_factory, acme.id, float(seconds), "full")
    await _record(session_factory, acme.id, 999.0, "incremental")

    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        full = await repo.get_recent_durations(acme.id, "full")
        incremental = await repo.get_recent_durations(acme.id, "incremental")
    assert incremental == [999.0]
    # Only the most recent window is considered; the oldest runs age out.
    assert len(full) == 10
    assert max(full) == 30.0
    assert min(full) == 21.0


# -- tenant API ------------------------------------------------------------


async def test_without_history_intervals_below_the_default_are_rejected(ctx):
    app, _sf, _acme = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"sync_interval_minutes": 10}
        )
        assert rejected.status_code == 422
        detail = rejected.json()["detail"]
        assert detail["min_safe_minutes"] == 30
        assert detail["basis"] == "default"
        assert "no measured sync history" in detail["message"]

        accepted = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"sync_interval_minutes": 45}
        )
        assert accepted.status_code == 200


async def test_interval_below_measured_floor_is_rejected_with_the_numbers(ctx):
    app, session_factory, acme = ctx
    await _record(session_factory, acme.id, 204.0, "full")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"sync_interval_minutes": 10}
        )
        assert rejected.status_code == 422
        detail = rejected.json()["detail"]
        assert detail["requested_minutes"] == 10
        assert detail["min_safe_minutes"] == 11
        assert detail["slowest_recent_seconds"] == 204.0
        assert "204" in detail["message"]
        assert "11 minutes" in detail["message"]

        at_floor = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"sync_interval_minutes": 11}
        )
        assert at_floor.status_code == 200
        assert at_floor.json()["sync_interval_minutes"] == 11


async def test_floor_follows_the_resulting_sync_mode(ctx):
    """History of cheap incremental runs must not license a full-sync interval."""
    app, session_factory, acme = ctx
    await _record(session_factory, acme.id, 20.0, "incremental")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.patch(
            "/tenants/acme", headers=HEADERS, json={"sync_interval_minutes": 6}
        )  # tenant is full: no full history -> default floor
        assert rejected.status_code == 422

        accepted = await client.patch(
            "/tenants/acme",
            headers=HEADERS,
            json={"sync_mode": "incremental", "sync_interval_minutes": 6},
        )
        assert accepted.status_code == 200  # ceil(20 * 3 / 60) = 1 -> absolute min 5


async def test_unrelated_patches_are_not_blocked_by_a_legacy_low_interval(ctx):
    app, session_factory, _acme = ctx
    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(app.state.settings.fernet_key))
        existing = await repo.get_by_slug("acme")
        existing.sync_interval_minutes = 5
        await repo.update(existing)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch("/tenants/acme", headers=HEADERS, json={"active": False})
    assert response.status_code == 200


async def test_create_and_put_enforce_the_floor_too(ctx):
    app, _sf, _acme = ctx
    body = {
        "slug": "beta",
        "bims_base_url": "https://b.example.com",
        "bims_api_key": "k",
        "shopify_shop_domain": "beta.myshopify.com",
        "shopify_access_token": "t",
        "shopify_webhook_secret": "w",
        "sync_interval_minutes": 5,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/tenants", headers=HEADERS, json=body)).status_code == 422
        ok = await client.post(
            "/tenants", headers=HEADERS, json=body | {"sync_interval_minutes": 30}
        )
        assert ok.status_code == 201
        put = await client.put("/tenants/beta", headers=HEADERS, json=body)
        assert put.status_code == 422


async def test_sync_status_exposes_the_recommended_minimum(ctx):
    app, session_factory, acme = ctx
    await _record(session_factory, acme.id, 204.0, "full")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = await client.get("/sync/acme/status", headers=HEADERS)
    interval = status.json()["sync_interval"]
    assert interval["configured_minutes"] == 30
    assert interval["min_safe_minutes"] == 11
    assert interval["slowest_recent_seconds"] == 204.0
    assert interval["sync_mode"] == SyncMode.FULL.value
