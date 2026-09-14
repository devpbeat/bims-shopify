"""Per-tenant `push_orders_to_bims` toggle.

Business decision (client CEO): for tenant `mystore`, BIMS is the single
source of truth. Shopify orders must NOT create sales or decrement stock in
BIMS; staff invoice manually in BIMS and the periodic sync feeds Shopify
inventory. When the flag is false, both webhook entry points
(per-tenant `/webhooks/shopify/{slug}` and app-level `/webhooks/shopify/app`)
must acknowledge 200, skip the BIMS Sale/reorder call entirely, log
`order_push_disabled`, and still record the event in `processed_events` for
idempotency/audit. Other tenants (flag true, the default) keep existing
behavior.
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
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import webhooks
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

API_SECRET = "app-client-secret"
SHOP = "mystore.myshopify.com"
TENANT_WEBHOOK_SECRET = "per-tenant-secret"


def _sign_body(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture
def push_orders_to_bims(request):
    """Value driven by `@pytest.mark.parametrize(..., indirect=True)`."""
    return getattr(request, "param", True)


@pytest.fixture
async def app_ctx(push_orders_to_bims):
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
                slug="mystore",
                bims_base_url="https://bims.example.com",
                bims_api_key="mystore_secretkey",
                shopify_shop_domain=SHOP,
                shopify_access_token="shpat_test",
                shopify_webhook_secret=TENANT_WEBHOOK_SECRET,
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
                push_orders_to_bims=push_orders_to_bims,
            )
        )

    yield app, settings, session_factory
    await engine.dispose()
    os.unlink(db_file.name)


def _order_payload(order_id: int) -> bytes:
    return json.dumps(
        {
            "id": order_id,
            "total_price": "10000.00",
            "line_items": [{"product_id": 1, "quantity": 1, "price": "10000.00"}],
        }
    ).encode("utf-8")


@pytest.mark.parametrize("push_orders_to_bims", [False], indirect=True)
@respx.mock
async def test_tenant_webhook_skips_bims_call_when_flag_disabled(app_ctx):
    app, settings, session_factory = app_ctx
    body = _order_payload(9001)
    signature = _sign_body(TENANT_WEBHOOK_SECRET, body)

    bims_route = respx.post("https://bims.example.com/api/sales/add.json").mock(
        return_value=httpx.Response(200, json={"data": {"id": 1}})
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/mystore",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/paid",
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert bims_route.call_count == 0

    async with session_factory() as session:
        tenant_repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        sync_repo = SqlAlchemySyncStateRepository(session)
        tenant = await tenant_repo.get_by_slug("mystore")
        status = await sync_repo.get_event_status(tenant.id, "shopify_webhook", "9001")

    assert status == "processed"


@pytest.mark.parametrize("push_orders_to_bims", [False], indirect=True)
@respx.mock
async def test_app_level_webhook_skips_bims_call_when_flag_disabled(app_ctx):
    app, settings, session_factory = app_ctx
    body = _order_payload(9002)
    signature = _sign_body(API_SECRET, body)

    bims_route = respx.post("https://bims.example.com/api/sales/add.json").mock(
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
    assert bims_route.call_count == 0

    async with session_factory() as session:
        tenant_repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        sync_repo = SqlAlchemySyncStateRepository(session)
        tenant = await tenant_repo.get_by_slug("mystore")
        status = await sync_repo.get_event_status(tenant.id, "shopify_webhook", "9002")

    assert status == "processed"


@pytest.mark.parametrize("push_orders_to_bims", [True], indirect=True)
@respx.mock
async def test_tenant_webhook_calls_bims_when_flag_enabled(app_ctx):
    app, settings, session_factory = app_ctx
    body = _order_payload(9003)
    signature = _sign_body(TENANT_WEBHOOK_SECRET, body)

    bims_route = respx.post("https://bims.example.com/api/sales/add.json").mock(
        return_value=httpx.Response(200, json={"data": {"id": 1}})
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/mystore",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/paid",
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert bims_route.call_count == 1

    async with session_factory() as session:
        tenant_repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        sync_repo = SqlAlchemySyncStateRepository(session)
        tenant = await tenant_repo.get_by_slug("mystore")
        status = await sync_repo.get_event_status(tenant.id, "shopify_webhook", "9003")

    assert status == "processed"


@pytest.mark.parametrize("push_orders_to_bims", [True], indirect=True)
@respx.mock
async def test_app_level_webhook_calls_bims_when_flag_enabled(app_ctx):
    app, settings, session_factory = app_ctx
    body = _order_payload(9004)
    signature = _sign_body(API_SECRET, body)

    bims_route = respx.post("https://bims.example.com/api/sales/add.json").mock(
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
    assert bims_route.call_count == 1

    async with session_factory() as session:
        tenant_repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        sync_repo = SqlAlchemySyncStateRepository(session)
        tenant = await tenant_repo.get_by_slug("mystore")
        status = await sync_repo.get_event_status(tenant.id, "shopify_webhook", "9004")

    assert status == "processed"
