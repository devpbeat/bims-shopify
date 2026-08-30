"""Integration tests for app-level Shopify webhooks and GDPR compliance endpoints.

Covers: POST /webhooks/shopify/app tenant resolution via X-Shopify-Shop-Domain
and HMAC verification against the app's single SHOPIFY_API_SECRET, and
POST /webhooks/shopify/compliance/{topic} HMAC verification + shop/redact
tenant deletion.
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

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import webhooks
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

API_SECRET = "app-client-secret"
SHOP = "acme.myshopify.com"


def _sign_body(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture
async def app_ctx():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="test-admin-token",
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
    app.include_router(webhooks.router)

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        await repo.create(
            Tenant(
                id=None,
                slug="acme",
                bims_base_url="https://bims.example.com",
                bims_api_key="acme_secretkey",
                shopify_shop_domain=SHOP,
                shopify_access_token="shpat_test",
                shopify_webhook_secret="per-tenant-secret",
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
        )

    yield app, settings, session_factory
    await engine.dispose()
    os.unlink(db_file.name)


@respx.mock
async def test_app_webhook_resolves_tenant_by_shop_domain_header(app_ctx):
    app, _settings, _ = app_ctx
    body = json.dumps({"id": 555, "total_price": "10.00", "line_items": []}).encode("utf-8")
    signature = _sign_body(API_SECRET, body)

    respx.post("https://bims.example.com/api/sales/add.json").mock(
        return_value=httpx.Response(200, json={"data": {"id": 1}})
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/app",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Shop-Domain": SHOP,
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


async def test_app_webhook_rejects_invalid_hmac(app_ctx):
    app, _, _ = app_ctx
    body = json.dumps({"id": 555}).encode("utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/app",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": "bad-signature",
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Shop-Domain": SHOP,
            },
        )

    assert resp.status_code == 401


async def test_app_webhook_unknown_shop_returns_ignored_200(app_ctx):
    app, _, _ = app_ctx
    body = json.dumps({"id": 555}).encode("utf-8")
    signature = _sign_body(API_SECRET, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/app",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Shop-Domain": "unknown-shop.myshopify.com",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


async def test_compliance_webhook_rejects_invalid_hmac(app_ctx):
    app, _, _ = app_ctx
    body = json.dumps({"shop_domain": SHOP}).encode("utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/compliance/shop/redact",
            content=body,
            headers={"X-Shopify-Hmac-Sha256": "bad-signature"},
        )

    assert resp.status_code == 401


async def test_compliance_shop_redact_deletes_tenant(app_ctx):
    app, settings, session_factory = app_ctx
    body = json.dumps({"shop_id": 1, "shop_domain": SHOP}).encode("utf-8")
    signature = _sign_body(API_SECRET, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/compliance/shop/redact",
            content=body,
            headers={"X-Shopify-Hmac-Sha256": signature},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("acme")
    assert tenant is None


async def test_compliance_customers_redact_returns_200_without_deleting_tenant(app_ctx):
    app, settings, session_factory = app_ctx
    body = json.dumps({"shop_id": 1, "shop_domain": SHOP, "customer": {"id": 1}}).encode("utf-8")
    signature = _sign_body(API_SECRET, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/compliance/customers/redact",
            content=body,
            headers={"X-Shopify-Hmac-Sha256": signature},
        )

    assert resp.status_code == 200

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("acme")
    assert tenant is not None
