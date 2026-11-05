"""Tenant-scoped reads of ``ops_jobs``."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bims_shopify.adapters.persistence.models import OpsJobModel
from bims_shopify.adapters.persistence.ops_job_repository import SqlAlchemyOpsJobReader

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


async def _add(factory, tenant_id, command, status, *, minutes_ago, result=None):
    async with factory() as session:
        session.add(
            OpsJobModel(
                tenant_id=tenant_id, command=command, options={}, status=status, result=result,
                created_at=NOW - timedelta(minutes=minutes_ago),
            )
        )
        await session.commit()


async def test_latest_is_the_newest_job_of_the_command_without_its_result(migrated_db):
    factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _add(factory, tenant.id, "missing_photos", "succeeded", minutes_ago=30, result={"a": 1})
    await _add(factory, tenant.id, "missing_photos", "running", minutes_ago=1)
    await _add(factory, tenant.id, "publish", "failed", minutes_ago=0)

    async with factory() as session:
        job = await SqlAlchemyOpsJobReader(session).latest(tenant.id, "missing_photos")

    assert job is not None and job.status == "running" and job.result is None


async def test_latest_succeeded_skips_newer_unsuccessful_jobs_and_carries_the_result(migrated_db):
    factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _add(factory, tenant.id, "missing_photos", "succeeded", minutes_ago=30, result={"a": 1})
    await _add(factory, tenant.id, "missing_photos", "failed", minutes_ago=1)

    async with factory() as session:
        job = await SqlAlchemyOpsJobReader(session).latest_succeeded(tenant.id, "missing_photos")

    assert job is not None and job.result == {"a": 1}


async def test_reads_never_cross_tenants(migrated_db):
    factory, make_tenant = migrated_db
    acme = await make_tenant()
    beta = await make_tenant(slug="beta", shopify_shop_domain="beta.myshopify.com")
    await _add(factory, beta.id, "missing_photos", "succeeded", minutes_ago=1, result={"b": 1})

    async with factory() as session:
        reader = SqlAlchemyOpsJobReader(session)
        assert await reader.latest(acme.id, "missing_photos") is None
        assert await reader.latest_succeeded(acme.id, "missing_photos") is None
