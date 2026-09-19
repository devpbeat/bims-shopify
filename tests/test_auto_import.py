"""Tests for AutoImportCoordinator: due/interval logic, single-flight skip,
disabled/inactive tenant skipping, publish-flag passthrough, and per-tenant
failure isolation."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import OpsJobModel
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ops.auto_import import AutoImportCoordinator
from bims_shopify.ops.job_runner import JobRunner


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


def _tenant_kwargs(slug: str, **overrides) -> dict:
    base = dict(
        id=None,
        slug=slug,
        bims_base_url="https://bims.example.com",
        bims_api_key=f"{slug}_key",
        shopify_shop_domain=f"{slug}.myshopify.com",
        shopify_access_token="shpat_test",
        shopify_webhook_secret="whsecret",
        shopify_location_id="gid://shopify/Location/1",
        bims_posale_id=1,
        bims_warehouse_id=1,
        bims_company_id=1,
        bims_currency_id=1,
        bims_payment_method_id=1,
        default_customer_contact_id=1,
        active=True,
        auto_import_products=True,
        auto_import_publish=False,
        auto_import_interval_minutes=360,
    )
    base.update(overrides)
    return base


@pytest.fixture
async def env():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    repo_root = Path(__file__).resolve().parents[1]
    await _run_migrations(settings.database_url, repo_root)

    job_runner = JobRunner(session_factory, settings)

    class RecordingJobRunner:
        """Wraps the real JobRunner so tests can assert on enqueued options
        without waiting for the (mocked-out) catalog import to actually run."""

        def __init__(self, inner: JobRunner) -> None:
            self._inner = inner
            self.started: list[tuple[int, str, dict]] = []

        def is_running(self, tenant_id: int, command: str) -> bool:
            return self._inner.is_running(tenant_id, command)

        async def start_job(self, tenant_id: int, command: str, options: dict):
            self.started.append((tenant_id, command, options))
            return await self._inner.start_job(tenant_id, command, options)

    recording_runner = RecordingJobRunner(job_runner)

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))

        async def make_tenant(**overrides) -> Tenant:
            return await repo.create(Tenant(**_tenant_kwargs(**overrides)))

        yield {
            "settings": settings,
            "session_factory": session_factory,
            "repo": repo,
            "make_tenant": make_tenant,
            "job_runner": recording_runner,
        }

    await engine.dispose()


async def _insert_finished_import_job(
    session_factory, tenant_id: int, *, trigger: str, created_at: datetime, status: str = "succeeded"
) -> None:
    async with session_factory() as session:
        job = OpsJobModel(
            tenant_id=tenant_id,
            command="import",
            options={"apply": True, "_trigger": trigger},
            status=status,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        await session.execute(
            update(OpsJobModel)
            .where(OpsJobModel.id == job.id)
            .values(created_at=created_at, finished_at=created_at)
        )
        await session.commit()


async def test_enqueues_when_due_and_no_prior_import(env):
    tenant = await env["make_tenant"](slug="acme")
    coordinator = AutoImportCoordinator(env["repo"], env["job_runner"], env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    assert len(env["job_runner"].started) == 1
    tenant_id, command, options = env["job_runner"].started[0]
    assert tenant_id == tenant.id
    assert command == "import"
    assert options == {"apply": True, "publish": False, "_trigger": "scheduler"}


async def test_skips_when_not_due(env):
    tenant = await env["make_tenant"](slug="acme", auto_import_interval_minutes=360)
    await _insert_finished_import_job(
        env["session_factory"],
        tenant.id,
        trigger="scheduler",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    coordinator = AutoImportCoordinator(env["repo"], env["job_runner"], env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    assert env["job_runner"].started == []


async def test_enqueues_when_interval_elapsed(env):
    tenant = await env["make_tenant"](slug="acme", auto_import_interval_minutes=60)
    await _insert_finished_import_job(
        env["session_factory"],
        tenant.id,
        trigger="scheduler",
        created_at=datetime.now(UTC) - timedelta(minutes=61),
    )
    coordinator = AutoImportCoordinator(env["repo"], env["job_runner"], env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    assert len(env["job_runner"].started) == 1


async def test_manual_import_does_not_count_as_due_marker(env):
    """A manually-triggered import (no scheduler marker) must not satisfy
    the auto-import interval -- only scheduler-triggered ones do."""
    tenant = await env["make_tenant"](slug="acme", auto_import_interval_minutes=360)
    await _insert_finished_import_job(
        env["session_factory"],
        tenant.id,
        trigger="manual",
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    coordinator = AutoImportCoordinator(env["repo"], env["job_runner"], env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    assert len(env["job_runner"].started) == 1


async def test_skips_when_job_already_running(env):
    tenant = await env["make_tenant"](slug="acme")
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_import(_tenant, _options, _progress):
        started.set()
        await release.wait()
        return {}

    import bims_shopify.ops.job_runner as job_runner_module

    job_runner_module._COMMAND_RUNNERS["import"] = slow_import
    try:
        real_runner = env["job_runner"]._inner
        coordinator = AutoImportCoordinator(env["repo"], real_runner, env["session_factory"])
        await coordinator.run_once()
        await started.wait()

        # Second tick while the job is still running should be skipped.
        await coordinator.run_once()
        await asyncio.sleep(0.05)

        async with env["session_factory"]() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(OpsJobModel).where(OpsJobModel.tenant_id == tenant.id, OpsJobModel.command == "import")
            )
            jobs = result.scalars().all()
        assert len(jobs) == 1

        release.set()
        await asyncio.sleep(0.05)
    finally:
        from bims_shopify.ops import catalog

        job_runner_module._COMMAND_RUNNERS["import"] = catalog.run_import


async def test_disabled_tenant_not_enqueued(env):
    await env["make_tenant"](slug="acme", auto_import_products=False)
    coordinator = AutoImportCoordinator(env["repo"], env["job_runner"], env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    assert env["job_runner"].started == []


async def test_inactive_tenant_not_enqueued(env):
    await env["make_tenant"](slug="acme", active=False)
    coordinator = AutoImportCoordinator(env["repo"], env["job_runner"], env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    assert env["job_runner"].started == []


async def test_publish_flag_is_forwarded(env):
    await env["make_tenant"](slug="acme", auto_import_publish=True)
    coordinator = AutoImportCoordinator(env["repo"], env["job_runner"], env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    _, _, options = env["job_runner"].started[0]
    assert options["publish"] is True


async def test_per_tenant_interval_is_respected_independently(env):
    tenant_slow = await env["make_tenant"](slug="slow", auto_import_interval_minutes=360)
    tenant_fast = await env["make_tenant"](slug="fast", auto_import_interval_minutes=5)
    await _insert_finished_import_job(
        env["session_factory"],
        tenant_slow.id,
        trigger="scheduler",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    await _insert_finished_import_job(
        env["session_factory"],
        tenant_fast.id,
        trigger="scheduler",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    coordinator = AutoImportCoordinator(env["repo"], env["job_runner"], env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    enqueued_tenant_ids = {tenant_id for tenant_id, _, _ in env["job_runner"].started}
    assert enqueued_tenant_ids == {tenant_fast.id}


async def test_one_tenant_failure_does_not_stop_others(env, monkeypatch):
    failing_tenant = await env["make_tenant"](slug="broken")
    healthy_tenant = await env["make_tenant"](slug="healthy")

    class FlakyJobRunner:
        def __init__(self, inner):
            self._inner = inner
            self.started: list[int] = []

        def is_running(self, tenant_id, command):
            return self._inner.is_running(tenant_id, command)

        async def start_job(self, tenant_id, command, options):
            if tenant_id == failing_tenant.id:
                raise RuntimeError("boom")
            self.started.append(tenant_id)
            return await self._inner.start_job(tenant_id, command, options)

    flaky_runner = FlakyJobRunner(env["job_runner"]._inner)
    coordinator = AutoImportCoordinator(env["repo"], flaky_runner, env["session_factory"])

    await coordinator.run_once()
    await asyncio.sleep(0.05)

    assert flaky_runner.started == [healthy_tenant.id]
