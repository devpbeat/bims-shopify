"""GET/PUT /api/portal/{slug}/warehouse-scope and /sync: the merchant picks which BIMS
warehouses feed the storefront, and starts a sync from the portal."""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.bims.client import BIMSAPIError
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import AuditLogModel, OpsJobModel
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import portal, portal_warehouse_scope
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ops.job_runner import JobConflictError

ADMIN = {"Authorization": "Bearer test-admin-token"}
OTHER = {"Authorization": "Bearer other-token"}

BIMS_WAREHOUSES = {
    "total": 3,
    "configured_ids": [24, 26, 27],
    "configured_ids_not_found": [],
    "notes": [],
    "warehouses": [
        {"id": 24, "name": "ARTIGAS", "synced": True},
        {"id": 26, "name": "ESPAÑA", "synced": True},
        {"id": 27, "name": "MAYORISTA", "synced": True},
        {"id": None, "name": "BROKEN ROW", "synced": False},
    ],
}


def _tenant(slug: str, **overrides) -> Tenant:
    base = dict(
        id=None, slug=slug, bims_base_url="https://bims.example.com", bims_api_key="k",
        shopify_shop_domain=f"{slug}.myshopify.com", shopify_access_token="t",
        shopify_webhook_secret="w", shopify_location_id="gid://shopify/Location/1",
        bims_posale_id=1, bims_warehouse_id=24, bims_warehouse_ids=[24, 26, 27],
        bims_company_id=1, bims_currency_id=1, bims_payment_method_id=1,
        default_customer_contact_id=1,
    )
    base.update(overrides)
    return Tenant(**base)


class _StubRunner:
    def __init__(self) -> None:
        self.started: list[tuple[int, str, dict]] = []
        self.conflict = False

    async def start_job(self, tenant_id, command, options):
        if self.conflict:
            raise JobConflictError("already running")
        self.started.append((tenant_id, command, options))

        class _Job:
            id = 91
            status = "queued"

        return _Job()


class _Scheduler:
    def __init__(self) -> None:
        self.rescheduled: list[str] = []

    def reschedule_tenant(self, tenant) -> None:
        self.rescheduled.append(tenant.slug)


@pytest.fixture
async def app(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="test-admin-token",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from alembic.config import Config

    from alembic import command

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = settings.database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    calls: list[str] = []

    async def fake_run_warehouses(tenant, options, progress=None):
        calls.append(tenant.slug)
        return BIMS_WAREHOUSES

    monkeypatch.setattr(portal_warehouse_scope, "run_warehouses", fake_run_warehouses)

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = factory
    app.state.job_runner = _StubRunner()
    app.state.scheduler = _Scheduler()
    app.state.bims_calls = calls
    app.include_router(portal.router)
    app.include_router(portal_warehouse_scope.router)
    async with factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        await repo.create(
            _tenant("acme", field_mappings={"keep_me": {"a": 1}})
        )
        await repo.create(
            _tenant(
                "scoped",
                field_mappings={
                    "stock_figure_rule": "warehouse_scope",
                    "stock_scope_warehouse_ids": [27, 99],
                },
            )
        )
        other = await repo.create(_tenant("other"))
        await repo.set_portal_token_hash(other.id, hashlib.sha256(b"other-token").hexdigest())
    yield app
    await engine.dispose()
    os.unlink(db_file.name)


async def _call(app, method, path, *, headers=ADMIN, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.request(method, f"/api/portal/{path}", headers=headers, **kwargs)


async def _load(app, slug):
    async with app.state.session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(app.state.settings.fernet_key))
        return await repo.get_by_slug(slug)


async def _audit(app, action):
    async with app.state.session_factory() as session:
        return list(await session.scalars(select(AuditLogModel).where(AuditLogModel.action == action)))


# -- GET --------------------------------------------------------------------


async def test_get_lists_bims_warehouses_with_synced_and_selected_flags(app):
    response = await _call(app, "GET", "scoped/warehouse-scope")

    assert response.status_code == 200
    body = response.json()
    assert body["rule"] == "warehouse_scope" and body["active"] is True
    assert body["selected_ids"] == [27, 99]
    assert body["warehouses"] == [
        {"id": 24, "name": "ARTIGAS", "synced": True, "selected": False},
        {"id": 26, "name": "ESPAÑA", "synced": True, "selected": False},
        {"id": 27, "name": "MAYORISTA", "synced": True, "selected": True},
    ]  # a row BIMS gave no id for cannot be selected, so it is not offered
    assert body["selected_ids_not_found"] == [99]


async def test_get_for_a_tenant_without_a_scope_reports_inactive(app):
    body = (await _call(app, "GET", "acme/warehouse-scope")).json()

    assert body["rule"] == "detail_or_total" and body["active"] is False
    assert body["selected_ids"] == []
    assert not any(w["selected"] for w in body["warehouses"])


async def test_get_requires_portal_auth_and_is_tenant_scoped(app):
    assert (await _call(app, "GET", "acme/warehouse-scope", headers={})).status_code == 401
    assert (await _call(app, "GET", "acme/warehouse-scope", headers=OTHER)).status_code == 401
    assert (await _call(app, "GET", "other/warehouse-scope", headers=OTHER)).status_code == 200
    assert app.state.bims_calls == ["other"]


async def test_get_answers_502_without_leaking_when_bims_fails(app, monkeypatch):
    async def boom(tenant, options, progress=None):
        raise BIMSAPIError("500", "secret internal detail")

    monkeypatch.setattr(portal_warehouse_scope, "run_warehouses", boom)

    response = await _call(app, "GET", "acme/warehouse-scope")

    assert response.status_code == 502
    assert "secret internal detail" not in response.text


# -- PUT --------------------------------------------------------------------


async def test_put_saves_the_scope_keeps_other_mappings_and_audits(app):
    response = await _call(app, "PUT", "acme/warehouse-scope", json={"warehouse_ids": [27]})

    assert response.status_code == 200
    body = response.json()
    assert body["rule"] == "warehouse_scope" and body["selected_ids"] == [27]
    assert [w["id"] for w in body["warehouses"] if w["selected"]] == [27]
    stored = await _load(app, "acme")
    assert stored.field_mappings == {
        "keep_me": {"a": 1},
        "stock_figure_rule": "warehouse_scope",
        "stock_scope_warehouse_ids": [27],
    }
    entries = await _audit(app, "portal.warehouse_scope_updated")
    assert len(entries) == 1
    assert entries[0].actor == "portal" and entries[0].tenant_id == stored.id
    assert entries[0].payload == {
        "previous_rule": "detail_or_total",
        "previous_warehouse_ids": [],
        "warehouse_ids": [27],
    }
    assert app.state.scheduler.rescheduled == ["acme"]


async def test_put_accepts_int_like_strings_and_dedupes(app):
    response = await _call(app, "PUT", "acme/warehouse-scope", json={"warehouse_ids": ["27", 26, 27]})

    assert response.status_code == 200
    assert response.json()["selected_ids"] == [27, 26]


@pytest.mark.parametrize(
    "ids", [[], ["abc"], [0], [-3], [True], [27.5], "27", None]
)
async def test_put_rejects_an_empty_or_malformed_selection_and_stores_nothing(app, ids):
    response = await _call(app, "PUT", "acme/warehouse-scope", json={"warehouse_ids": ids})

    assert response.status_code == 422
    assert (await _load(app, "acme")).field_mappings == {"keep_me": {"a": 1}}
    assert await _audit(app, "portal.warehouse_scope_updated") == []


async def test_put_rejects_an_id_bims_does_not_list(app):
    """A typo'd id would publish zero for the whole catalog: it must never be saved."""
    response = await _call(app, "PUT", "acme/warehouse-scope", json={"warehouse_ids": [27, 28]})

    assert response.status_code == 422
    assert response.json()["detail"]["unknown_ids"] == [28]
    assert (await _load(app, "acme")).field_mappings == {"keep_me": {"a": 1}}


async def test_put_does_not_save_when_bims_cannot_confirm_the_ids(app, monkeypatch):
    async def boom(tenant, options, progress=None):
        raise BIMSAPIError("500", "x")

    monkeypatch.setattr(portal_warehouse_scope, "run_warehouses", boom)

    response = await _call(app, "PUT", "acme/warehouse-scope", json={"warehouse_ids": [27]})

    assert response.status_code == 502
    assert (await _load(app, "acme")).field_mappings == {"keep_me": {"a": 1}}


async def test_put_with_an_unchanged_selection_is_not_audited_again(app):
    await _call(app, "PUT", "acme/warehouse-scope", json={"warehouse_ids": [27]})
    await _call(app, "PUT", "acme/warehouse-scope", json={"warehouse_ids": [27]})

    assert len(await _audit(app, "portal.warehouse_scope_updated")) == 1


async def test_put_is_tenant_scoped(app):
    response = await _call(app, "PUT", "acme/warehouse-scope", headers=OTHER, json={"warehouse_ids": [27]})

    assert response.status_code == 401
    assert (await _load(app, "acme")).field_mappings == {"keep_me": {"a": 1}}


# -- sync -------------------------------------------------------------------


async def test_post_sync_starts_the_ops_sync_job_for_this_tenant_only(app):
    response = await _call(app, "POST", "acme/warehouse-scope/sync")

    assert response.status_code == 202
    assert response.json() == {"job_id": 91, "status": "queued"}
    tenant = await _load(app, "acme")
    assert app.state.job_runner.started == [
        (tenant.id, "sync", {"dry_run": False, "full": False, "force": False})
    ]


async def test_post_sync_answers_409_when_one_is_already_running(app):
    app.state.job_runner.conflict = True

    assert (await _call(app, "POST", "acme/warehouse-scope/sync")).status_code == 409


async def test_post_sync_requires_portal_auth(app):
    assert (await _call(app, "POST", "acme/warehouse-scope/sync", headers=OTHER)).status_code == 401
    assert app.state.job_runner.started == []


async def _job(app, tenant_id, status, command="sync", **fields):
    async with app.state.session_factory() as session:
        session.add(
            OpsJobModel(
                tenant_id=tenant_id, command=command, options={}, status=status,
                created_at=datetime.now(UTC), **fields,
            )
        )
        await session.commit()


async def test_get_sync_state_never_running_succeeded_failed(app):
    tenant = await _load(app, "acme")
    assert (await _call(app, "GET", "acme/warehouse-scope/sync")).json()["status"] == "never"

    await _job(app, tenant.id, "running", progress_done=2, progress_total=10, message="pulling")
    running = (await _call(app, "GET", "acme/warehouse-scope/sync")).json()
    assert running["status"] == "running"
    assert (running["progress_done"], running["progress_total"]) == (2, 10)
    assert running["message"] == "pulling"

    await _job(app, tenant.id, "succeeded", finished_at=datetime.now(UTC))
    assert (await _call(app, "GET", "acme/warehouse-scope/sync")).json()["status"] == "succeeded"

    await _job(app, tenant.id, "failed", error="secret traceback")
    failed = await _call(app, "GET", "acme/warehouse-scope/sync")
    assert failed.json()["status"] == "failed"
    assert "secret traceback" not in failed.text


async def test_get_sync_state_ignores_other_commands_and_tenants(app):
    acme, other = await _load(app, "acme"), await _load(app, "other")
    await _job(app, acme.id, "running", command="missing_photos")
    await _job(app, other.id, "running")

    assert (await _call(app, "GET", "acme/warehouse-scope/sync")).json()["status"] == "never"
