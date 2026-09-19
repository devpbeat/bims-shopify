"""End-to-end coverage for the Pagopar "manual payment" flow:

orders/create webhook (gateway match) -> payment link persisted ->
Pagopar callback -> verified -> Shopify order marked paid, plus the
merchant-portal payments listing.

Uses the same app-context fixture pattern as test_app_level_webhooks.py,
with the Pagopar SDK's HTTP calls mocked via respx (never a live Pagopar
call).
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
from bims_shopify.api import payments, portal, webhooks
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

API_SECRET = "app-client-secret"
SHOP = "mystore.myshopify.com"


def _sign_body(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


async def _make_tenant(session_factory, settings, **overrides) -> Tenant:
    base = dict(
        id=None,
        slug="mystore",
        bims_base_url="https://bims.example.com",
        bims_api_key="mystore_secretkey",
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
        payment_provider=PaymentProvider.PAGOPAR,
        provider_config={"public_key": "pub-123", "private_key": "priv-456"},
        field_mappings={},
        active=True,
        push_orders_to_bims=False,
    )
    base.update(overrides)
    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        return await repo.create(Tenant(**base))


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
    app.include_router(payments.router)
    app.include_router(portal.router)

    yield app, settings, session_factory
    await engine.dispose()
    os.unlink(db_file.name)


def _order_payload(order_id=555, total="150000.00", gateway="Pagopar"):
    return {
        "id": order_id,
        "total_price": total,
        "currency": "PYG",
        "line_items": [],
        "payment_gateway_names": [gateway],
    }


@respx.mock
async def test_orders_create_webhook_creates_payment_link_for_matching_gateway(app_ctx):
    app, settings, session_factory = app_ctx
    await _make_tenant(session_factory, settings)

    create_route = respx.post(
        "https://api.pagopar.com/api/comercios/2.0/iniciar-transaccion"
    ).mock(
        return_value=httpx.Response(
            200, json={"respuesta": True, "resultado": [{"data": "hash-555", "pedido": "1"}]}
        )
    )

    body = json.dumps(_order_payload()).encode("utf-8")
    signature = _sign_body(API_SECRET, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/app",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/create",
                "X-Shopify-Shop-Domain": SHOP,
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 200
    assert create_route.called

    async with session_factory() as session:
        from bims_shopify.adapters.persistence.payment_intent_repository import (
            SqlAlchemyPaymentIntentRepository,
        )

        repo = SqlAlchemyPaymentIntentRepository(session)
        intents = await repo.list_recent(1)

    assert len(intents) == 1
    assert intents[0]["order_id"] == "555"
    assert intents[0]["checkout_url"] == "https://www.pagopar.com/pagos/hash-555"
    assert intents[0]["amount"] == 150000


@respx.mock
async def test_orders_create_webhook_ignores_non_matching_gateway(app_ctx):
    app, settings, session_factory = app_ctx
    await _make_tenant(session_factory, settings)

    create_route = respx.post(
        "https://api.pagopar.com/api/comercios/2.0/iniciar-transaccion"
    ).mock(return_value=httpx.Response(200, json={"respuesta": True, "resultado": [{"data": "x"}]}))

    body = json.dumps(_order_payload(gateway="Manual")).encode("utf-8")
    signature = _sign_body(API_SECRET, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/app",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/create",
                "X-Shopify-Shop-Domain": SHOP,
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 200
    assert not create_route.called


@respx.mock
async def test_orders_create_webhook_ignores_non_pagopar_tenant(app_ctx):
    app, settings, session_factory = app_ctx
    await _make_tenant(session_factory, settings, payment_provider=PaymentProvider.BANCARD)

    create_route = respx.post(
        "https://api.pagopar.com/api/comercios/2.0/iniciar-transaccion"
    ).mock(return_value=httpx.Response(200, json={"respuesta": True, "resultado": [{"data": "x"}]}))

    body = json.dumps(_order_payload()).encode("utf-8")
    signature = _sign_body(API_SECRET, body)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/shopify/app",
            content=body,
            headers={
                "X-Shopify-Hmac-Sha256": signature,
                "X-Shopify-Topic": "orders/create",
                "X-Shopify-Shop-Domain": SHOP,
                "Content-Type": "application/json",
            },
        )

    assert resp.status_code == 200
    assert not create_route.called


async def _seed_intent(session_factory, tenant_id, order_id="555", provider_reference="hash-555"):
    from bims_shopify.domain.payment import PaymentIntent

    async with session_factory() as session:
        from bims_shopify.adapters.persistence.payment_intent_repository import (
            SqlAlchemyPaymentIntentRepository,
        )

        repo = SqlAlchemyPaymentIntentRepository(session)
        await repo.save(
            tenant_id,
            "pagopar",
            PaymentIntent(
                tenant_slug="mystore",
                order_id=order_id,
                amount=150000,
                currency="PYG",
                checkout_url=f"https://www.pagopar.com/pagos/{provider_reference}",
                provider_reference=provider_reference,
                status="pending",
            ),
        )


@respx.mock
async def test_callback_confirmed_marks_order_paid_once(app_ctx, monkeypatch):
    app, settings, session_factory = app_ctx
    tenant = await _make_tenant(session_factory, settings)
    await _seed_intent(session_factory, tenant.id)

    respx.post("https://api.pagopar.com/api/pedidos/1.1/traer").mock(
        return_value=httpx.Response(
            200, json={"respuesta": True, "resultado": [{"pagado": True, "cancelado": False}]}
        )
    )

    marked: list[str] = []

    async def fake_mark_order_as_paid(self, tenant_arg, order_id):
        marked.append(order_id)

    from bims_shopify.adapters.shopify.client import ShopifyClient

    monkeypatch.setattr(ShopifyClient, "mark_order_as_paid", fake_mark_order_as_paid)

    callback_payload = {
        "resultado": [{"hash_pedido": "hash-555", "pagado": True, "cancelado": False}]
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/payments/pagopar/mystore/callback", json=callback_payload)
        second = await client.post("/payments/pagopar/mystore/callback", json=callback_payload)

    assert first.status_code == 200
    assert first.json()["status"] == "paid"
    assert second.json()["status"] == "paid"
    assert marked == ["555"]  # only marked paid once, despite two callback deliveries


@respx.mock
async def test_callback_verification_failure_does_not_mark_paid(app_ctx, monkeypatch):
    app, settings, session_factory = app_ctx
    tenant = await _make_tenant(session_factory, settings)
    await _seed_intent(session_factory, tenant.id)

    respx.post("https://api.pagopar.com/api/pedidos/1.1/traer").mock(
        return_value=httpx.Response(
            200, json={"respuesta": True, "resultado": [{"pagado": False, "cancelado": True}]}
        )
    )

    marked: list[str] = []

    async def fake_mark_order_as_paid(self, tenant_arg, order_id):
        marked.append(order_id)

    from bims_shopify.adapters.shopify.client import ShopifyClient

    monkeypatch.setattr(ShopifyClient, "mark_order_as_paid", fake_mark_order_as_paid)

    callback_payload = {
        "resultado": [{"hash_pedido": "hash-555", "pagado": False, "cancelado": True}]
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/payments/pagopar/mystore/callback", json=callback_payload)

    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert marked == []


async def test_portal_payments_listing_is_auth_scoped(app_ctx):
    app, settings, session_factory = app_ctx
    tenant = await _make_tenant(session_factory, settings)
    await _seed_intent(session_factory, tenant.id)

    async with session_factory() as session:
        from bims_shopify.adapters.persistence.tenant_repository import (
            SqlAlchemyTenantRepository as _Repo,
        )

        repo = _Repo(session, SecretBox(settings.fernet_key))
        await repo.set_portal_token_hash(
            tenant.id, hashlib.sha256(b"portal-token").hexdigest()
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/api/portal/mystore/payments")
        wrong_token = await client.get(
            "/api/portal/mystore/payments", headers={"Authorization": "Bearer nope"}
        )
        authorized = await client.get(
            "/api/portal/mystore/payments", headers={"Authorization": "Bearer portal-token"}
        )

    assert unauthorized.status_code == 401
    assert wrong_token.status_code == 401
    assert authorized.status_code == 200
    payments_list = authorized.json()["payments"]
    assert len(payments_list) == 1
    assert payments_list[0]["order_id"] == "555"
