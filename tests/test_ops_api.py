"""Integration tests for the /ops background-job API.

Covers: job lifecycle (queued -> running -> succeeded) with progress and
result persisted, single-flight 409 for overlapping same-tenant/command
runs, wipe requiring options.confirm, startup interrupted-marking, and
admin auth.
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
async def app_and_tenant():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    repo_root = Path(__file__).resolve().parents[1]
    await _run_migrations(settings.database_url, repo_root)

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.job_runner = JobRunner(session_factory, settings)
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

    yield app, created, session_factory
    await engine.dispose()
    os.unlink(db_file.name)


async def _wait_for_status(session_factory, job_id: int, statuses: set[str], timeout: float = 5.0) -> OpsJobModel:
    from sqlalchemy import select

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with session_factory() as session:
            result = await session.execute(select(OpsJobModel).where(OpsJobModel.id == job_id))
            job = result.scalar_one()
            if job.status in statuses:
                return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {statuses}, last status={job.status}")


def _fake_status_runner(calls: list[str]):
    async def _run(tenant, options, progress):
        calls.append("start")
        if progress is not None:
            await progress(1, 2, "half done")
        await asyncio.sleep(0)
        calls.append("end")
        return {"products": 3, "variants": 5}

    return _run


async def test_run_job_requires_admin_auth(app_and_tenant):
    app, _tenant, _sf = app_and_tenant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ops/acme/run", json={"command": "status"})
    assert response.status_code == 401


async def test_run_job_lifecycle_reaches_succeeded_with_result(app_and_tenant, monkeypatch):
    app, _tenant, session_factory = app_and_tenant
    calls: list[str] = []
    monkeypatch.setitem(
        __import__("bims_shopify.ops.job_runner", fromlist=["_COMMAND_RUNNERS"])._COMMAND_RUNNERS,
        "status",
        _fake_status_runner(calls),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/acme/run",
            json={"command": "status", "options": {}},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        assert response.json()["status"] in ("queued", "running")

        job = await _wait_for_status(session_factory, job_id, {"succeeded", "failed"})
        assert job.status == "succeeded"
        assert job.result == {"products": 3, "variants": 5}
        assert job.started_at is not None
        assert job.finished_at is not None

        detail_response = await client.get(
            f"/ops/acme/jobs/{job_id}", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        assert detail_response.status_code == 200
        body = detail_response.json()
        assert body["status"] == "succeeded"
        assert body["result"] == {"products": 3, "variants": 5}

        list_response = await client.get(
            "/ops/acme/jobs", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        assert list_response.status_code == 200
        listed = list_response.json()
        assert len(listed) == 1
        assert "result" not in listed[0]

    assert calls == ["start", "end"]


async def test_run_job_single_flight_returns_409(app_and_tenant, monkeypatch):
    app, _tenant, session_factory = app_and_tenant

    gate = asyncio.Event()

    async def _slow_runner(tenant, options, progress):
        await gate.wait()
        return {"ok": True}

    monkeypatch.setitem(
        __import__("bims_shopify.ops.job_runner", fromlist=["_COMMAND_RUNNERS"])._COMMAND_RUNNERS,
        "status",
        _slow_runner,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/ops/acme/run",
            json={"command": "status"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert first.status_code == 200
        # Let the background task actually start and acquire the lock.
        await asyncio.sleep(0.05)

        second = await client.post(
            "/ops/acme/run",
            json={"command": "status"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert second.status_code == 409

        gate.set()
        await _wait_for_status(session_factory, first.json()["job_id"], {"succeeded", "failed"})


async def test_wipe_without_confirm_is_422(app_and_tenant):
    app, _tenant, _sf = app_and_tenant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/acme/run",
            json={"command": "wipe", "options": {}},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert response.status_code == 422


async def test_import_dry_run_vs_apply_options_pass_through(app_and_tenant, monkeypatch):
    app, _tenant, session_factory = app_and_tenant
    seen_options: list[dict] = []

    async def _capture_runner(tenant, options, progress):
        seen_options.append(options)
        return {"products": 0}

    monkeypatch.setitem(
        __import__("bims_shopify.ops.job_runner", fromlist=["_COMMAND_RUNNERS"])._COMMAND_RUNNERS,
        "import",
        _capture_runner,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/acme/run",
            json={"command": "import", "options": {"apply": True, "only_with_stock": True, "limit": 5}},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert response.status_code == 200
        await _wait_for_status(session_factory, response.json()["job_id"], {"succeeded", "failed"})

    assert seen_options == [{"apply": True, "publish": False, "only_with_stock": True, "limit": 5}]


async def test_startup_marks_running_jobs_interrupted(app_and_tenant):
    _app, tenant, session_factory = app_and_tenant

    async with session_factory() as session:
        job = OpsJobModel(tenant_id=tenant.id, command="import", options={}, status="running")
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    count = await JobRunner.mark_interrupted_on_startup(session_factory)
    assert count == 1

    from sqlalchemy import select

    async with session_factory() as session:
        result = await session.execute(select(OpsJobModel).where(OpsJobModel.id == job_id))
        refreshed = result.scalar_one()
        assert refreshed.status == "interrupted"
        assert refreshed.finished_at is not None


async def test_run_job_unknown_tenant_404(app_and_tenant):
    app, _tenant, _sf = app_and_tenant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/does-not-exist/run",
            json={"command": "status"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert response.status_code == 404


async def test_run_job_failure_records_error(app_and_tenant, monkeypatch):
    app, _tenant, session_factory = app_and_tenant

    async def _boom(tenant, options, progress):
        raise ValueError("kaboom")

    monkeypatch.setitem(
        __import__("bims_shopify.ops.job_runner", fromlist=["_COMMAND_RUNNERS"])._COMMAND_RUNNERS,
        "status",
        _boom,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ops/acme/run",
            json={"command": "status"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        job_id = response.json()["job_id"]
        job = await _wait_for_status(session_factory, job_id, {"succeeded", "failed"})

    assert job.status == "failed"
    assert "kaboom" in job.error
