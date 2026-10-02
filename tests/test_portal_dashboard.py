"""Tests for the portal dashboard endpoint and rekey-report obsolescence.

Context: the portal used to land on the rekey conflict report by default,
but the catalog is now wipe+rebuilt, so old conflicts are meaningless once
a wipe/import has happened after the report was generated. These tests
cover the new `/dashboard` endpoint shape and the obsolescence rule on
`/rekey-report`.
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

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import AuditLogModel, RekeyReportModel
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import portal, tenants
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

ADMIN_TOKEN = "test-admin-token"


async def _make_tenant(repo: SqlAlchemyTenantRepository, slug: str, shop_domain: str) -> Tenant:
    tenant = Tenant(
        id=None,
        slug=slug,
        bims_base_url="https://bims.example.com",
        bims_api_key="secretkey",
        shopify_shop_domain=shop_domain,
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
        auto_import_products=True,
        auto_import_interval_minutes=120,
        auto_import_only_with_stock=True,
    )
    return await repo.create(tenant)


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
    app.include_router(portal.router)

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        acme = await _make_tenant(repo, "acme", "acme.myshopify.com")
        await session.commit()

    yield app, session_factory, acme
    await engine.dispose()
    os.unlink(db_file.name)


async def _mint_portal_token(app: FastAPI, slug: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/tenants/{slug}/portal-token",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert response.status_code == 200
    return response.json()["portal_token"]


async def test_dashboard_returns_expected_shape_with_admin_token(app_and_tenant):
    app, session_factory, acme = app_and_tenant

    async with session_factory() as session:
        sync_state_repo = SqlAlchemySyncStateRepository(session)
        ran_at = datetime.now(UTC)
        await sync_state_repo.set_last_run(acme.id, ran_at)
        await sync_state_repo.set_last_run_summary(
            acme.id,
            {"status": "ok", "matched": 10, "updated": 10, "duration_seconds": 5.5, "ran_at": ran_at.isoformat()},
        )

        audit = SqlAlchemyAuditLogger(session)
        await audit.log(
            actor="system",
            action="catalog.reconciliation",
            entity="catalog",
            tenant_id=acme.id,
            payload={
                "shopify_products": 100,
                "shopify_variants": 150,
                "matched_skus": 90,
                "bims_not_in_shopify": 5,
            },
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/dashboard",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "acme"
    assert body["last_sync"]["status"] == "ok"
    assert body["last_sync"]["matched"] == 10
    assert body["last_error"] is None
    assert body["auto_import"] == {
        "enabled": True,
        "interval_minutes": 120,
        "only_with_stock": True,
    }
    assert body["catalog"]["shopify_products"] == 100
    assert body["catalog"]["bims_not_in_shopify"] == 5


async def test_dashboard_requires_portal_auth(app_and_tenant):
    app, _session_factory, _acme = app_and_tenant
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/portal/acme/dashboard")
    assert response.status_code == 401


async def test_dashboard_works_with_portal_token(app_and_tenant):
    app, _session_factory, _acme = app_and_tenant
    token = await _mint_portal_token(app, "acme")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/dashboard", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["catalog"]["shopify_products"] is None
    assert "hint" in body["catalog"]


async def test_rekey_report_obsolete_when_wipe_happened_after_report(app_and_tenant):
    app, session_factory, acme = app_and_tenant

    old_report_time = datetime.now(UTC) - timedelta(days=10)
    async with session_factory() as session:
        session.add(
            RekeyReportModel(tenant_id=acme.id, payload={"total_variants": 1}, created_at=old_report_time)
        )
        session.add(
            AuditLogModel(
                tenant_id=acme.id,
                actor="system",
                action="catalog.wipe",
                entity="catalog",
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()

    token = await _mint_portal_token(app, "acme")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/rekey-report", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["obsolete"] is True


async def test_rekey_report_not_obsolete_when_newer_than_wipe(app_and_tenant):
    app, session_factory, acme = app_and_tenant

    async with session_factory() as session:
        session.add(
            AuditLogModel(
                tenant_id=acme.id,
                actor="system",
                action="catalog.wipe",
                entity="catalog",
                created_at=datetime.now(UTC) - timedelta(days=10),
            )
        )
        session.add(
            RekeyReportModel(
                tenant_id=acme.id,
                payload={"total_variants": 1},
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()

    token = await _mint_portal_token(app, "acme")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/rekey-report", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["obsolete"] is False
