"""Tests for the audit log slice: writer, fire-and-forget isolation, emit
points (tenant CRUD, portal-token mint, portal rekey resolutions, sync runs,
webhook receipt/order_push_disabled), and the two read endpoints
(admin `/audit`, portal `/api/portal/{slug}/audit`).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import AuditLogModel
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import audit, portal, sync, tenants, webhooks
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

ADMIN_TOKEN = "test-admin-token"
API_SECRET = "app-client-secret"


async def _make_tenant(repo: SqlAlchemyTenantRepository, slug: str, **overrides) -> Tenant:
    defaults = dict(
        id=None,
        slug=slug,
        bims_base_url="https://bims.example.com",
        bims_api_key="secretkey",
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
        reorder_threshold=0.0,
        reorder_strategy=ReorderStrategy.NONE,
        payment_provider=PaymentProvider.BANCARD,
        provider_config={},
        field_mappings={},
        active=True,
        push_orders_to_bims=True,
    )
    defaults.update(overrides)
    return await repo.create(Tenant(**defaults))


@pytest.fixture
async def app_ctx():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
        shopify_api_secret=API_SECRET,
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
    app.include_router(sync.router)
    app.include_router(webhooks.router)
    app.include_router(audit.router)

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        acme = await _make_tenant(repo, "acme")
        other = await _make_tenant(repo, "other")

    yield app, acme, other
    await engine.dispose()
    os.unlink(db_file.name)


async def _entries(app: FastAPI) -> list[AuditLogModel]:
    async with app.state.session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(AuditLogModel).order_by(AuditLogModel.id))
        return list(result.scalars().all())


# --- writer isolation -------------------------------------------------


async def test_audit_log_failure_is_swallowed_not_raised():
    class ExplodingSession:
        def add(self, _model):
            pass

        async def commit(self):
            raise RuntimeError("db is down")

        async def rollback(self):
            pass

    logger = SqlAlchemyAuditLogger(ExplodingSession())
    # Must not raise: a broken audit write must never break the caller.
    await logger.log(actor="system", action="test.action", entity="thing")


# --- emit points --------------------------------------------------------


async def test_tenant_create_writes_audit_entry(app_ctx):
    app, _acme, _other = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/tenants",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={
                "slug": "newco",
                "bims_base_url": "https://bims.example.com",
                "bims_api_key": "k",
                "shopify_shop_domain": "newco.myshopify.com",
                "shopify_access_token": "t",
                "shopify_webhook_secret": "s",
            },
        )
    assert response.status_code == 201
    entries = await _entries(app)
    matching = [e for e in entries if e.action == "tenant.create" and e.entity_id == "newco"]
    assert len(matching) == 1
    assert matching[0].actor == "admin"


async def test_tenant_update_and_delete_write_audit_entries(app_ctx):
    app, acme, _other = app_ctx
    transport = ASGITransport(app=app)
    body = {
        "slug": "acme",
        "bims_base_url": "https://bims.example.com",
        "bims_api_key": "k",
        "shopify_shop_domain": "acme.myshopify.com",
        "shopify_access_token": "t",
        "shopify_webhook_secret": "s",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        update_resp = await client.put(
            "/tenants/acme", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, json=body
        )
        assert update_resp.status_code == 200
        delete_resp = await client.delete(
            "/tenants/acme", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        assert delete_resp.status_code == 204

    entries = await _entries(app)
    actions = [e.action for e in entries if e.tenant_id == acme.id]
    assert "tenant.update" in actions
    assert "tenant.delete" in actions


async def test_portal_token_mint_writes_audit_without_leaking_token(app_ctx):
    app, _acme, _other = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/tenants/acme/portal-token", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
    assert response.status_code == 200
    token = response.json()["portal_token"]

    entries = await _entries(app)
    matching = [e for e in entries if e.action == "tenant.portal_token_mint"]
    assert len(matching) == 1
    assert matching[0].actor == "admin"
    # Compact payload only — the plaintext token must never be persisted.
    dumped = json.dumps(matching[0].payload or {})
    assert token not in dumped


async def test_sync_dry_run_and_completed_write_audit(app_ctx):
    app, _acme, _other = app_ctx
    transport = ASGITransport(app=app)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://bims.example.com/api/products/index.json").mock(
            return_value=httpx.Response(200, json={"status": "ok", "count": "0", "data": []})
        )
        mock.post("https://bims.example.com/api/products_stocks/stock_fenicio.json").mock(
            return_value=httpx.Response(200, json={"status": "OK", "data": {"stockPorSku": []}})
        )
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            dry = await client.post(
                "/sync/acme/run", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
            )
            assert dry.status_code == 200
            real = await client.post(
                "/sync/acme/run",
                params={"dry_run": "false"},
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
            assert real.status_code == 200

    entries = await _entries(app)
    actions = [e.action for e in entries]
    assert "sync.dry_run" in actions
    assert "sync.completed" in actions


async def test_webhook_order_received_and_push_disabled_write_audit(app_ctx):
    app, _acme, _other = app_ctx

    async with app.state.session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(app.state.settings.fernet_key))
        tenant = await repo.get_by_slug("acme")
        tenant.push_orders_to_bims = False
        await repo.update(tenant)

    body = json.dumps({"id": 12345, "total_price": "10.00", "line_items": []}).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(b"whsecret", body, hashlib.sha256).digest()
    ).decode("utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/shopify/acme",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/create",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 200

    entries = await _entries(app)
    actions = [e.action for e in entries]
    assert "webhook.order_received" in actions
    assert "webhook.order_push_disabled" in actions


# --- read endpoints -------------------------------------------------


async def test_admin_audit_requires_admin_auth(app_ctx):
    app, _acme, _other = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/audit")
    assert response.status_code == 401


async def test_admin_audit_filters_by_tenant_and_paginates(app_ctx):
    app, acme, other = app_ctx
    async with app.state.session_factory() as session:
        logger = SqlAlchemyAuditLogger(session)
        for i in range(3):
            await logger.log(
                actor="system", action="test.acme", entity="thing", tenant_id=acme.id, entity_id=str(i)
            )
        await logger.log(actor="system", action="test.other", entity="thing", tenant_id=other.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        all_resp = await client.get(
            "/audit", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        assert all_resp.status_code == 200
        assert len(all_resp.json()["entries"]) == 4

        scoped_resp = await client.get(
            "/audit",
            params={"tenant": "acme"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert scoped_resp.status_code == 200
        scoped_entries = scoped_resp.json()["entries"]
        assert len(scoped_entries) == 3
        assert all(e["action"] == "test.acme" for e in scoped_entries)

        limited_resp = await client.get(
            "/audit",
            params={"tenant": "acme", "limit": 1},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert len(limited_resp.json()["entries"]) == 1

        clamped_resp = await client.get(
            "/audit",
            params={"limit": 10_000},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert clamped_resp.status_code == 200  # limit silently clamped, not rejected


async def test_admin_audit_unknown_tenant_404s(app_ctx):
    app, _acme, _other = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/audit",
            params={"tenant": "does-not-exist"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert response.status_code == 404


async def test_portal_audit_is_scoped_to_authenticated_tenant(app_ctx):
    app, acme, other = app_ctx
    async with app.state.session_factory() as session:
        logger = SqlAlchemyAuditLogger(session)
        await logger.log(actor="system", action="test.acme", entity="thing", tenant_id=acme.id)
        await logger.log(actor="system", action="test.other", entity="thing", tenant_id=other.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mint = await client.post(
            "/tenants/acme/portal-token", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        token = mint.json()["portal_token"]

        response = await client.get(
            "/api/portal/acme/audit", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    entries = response.json()["entries"]
    # Only acme's entries are visible — never other's, and never via a
    # tenant query param (the portal route takes none).
    assert all(e["action"] != "test.other" for e in entries)
    assert any(e["action"] == "test.acme" for e in entries)


async def test_portal_audit_requires_valid_token(app_ctx):
    app, _acme, _other = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/audit", headers={"Authorization": "Bearer wrong-token"}
        )
    assert response.status_code == 401
