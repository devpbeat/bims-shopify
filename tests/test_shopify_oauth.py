"""Integration tests for the Shopify OAuth install flow.

Covers: GET /shopify/install redirect + state persistence, GET
/shopify/callback HMAC verification (pass/fail), state nonce mismatch, and
tenant upsert (create on first install, active=False until BIMS-configured).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
from bims_shopify.api import shopify_oauth, tenants
from bims_shopify.config import Settings

API_KEY = "test-client-id"
API_SECRET = "test-client-secret"
SHOP = "acme.myshopify.com"


def _sign_query(params: dict[str, str], secret: str) -> str:
    filtered = {k: v for k, v in params.items() if k not in ("hmac", "signature")}
    message = "&".join(f"{k}={v}" for k, v in sorted(filtered.items()))
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.fixture
async def app_ctx():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="test-admin-token",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
        shopify_api_key=API_KEY,
        shopify_api_secret=API_SECRET,
        public_base_url="https://mystoresync.ignitesolutions.click",
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
    app.include_router(shopify_oauth.router)
    app.include_router(tenants.router)

    yield app, settings, session_factory

    await engine.dispose()
    os.unlink(db_file.name)


async def test_install_redirects_to_authorize_url_and_persists_state(app_ctx):
    app, _settings, _ = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/shopify/install", params={"shop": SHOP}, follow_redirects=False)

    assert resp.status_code == 302
    location = resp.headers["location"]
    parsed = urlparse(location)
    assert parsed.netloc == SHOP
    assert parsed.path == "/admin/oauth/authorize"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == [API_KEY]
    assert qs["redirect_uri"] == ["https://mystoresync.ignitesolutions.click/shopify/callback"]
    assert "state" in qs
    assert "read_products" in qs["scope"][0]


async def test_install_rejects_invalid_shop_domain(app_ctx):
    app, _, _ = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/shopify/install", params={"shop": "not-a-shop"})
    assert resp.status_code == 400


async def _get_state(app, shop: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/shopify/install", params={"shop": shop}, follow_redirects=False)
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    return qs["state"][0]


@respx.mock
async def test_callback_valid_hmac_and_state_upserts_tenant(app_ctx):
    app, settings, session_factory = app_ctx
    state = await _get_state(app, SHOP)

    respx.post(f"https://{SHOP}/admin/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "shpat_new_token", "scope": "read_products"})
    )
    respx.post(f"https://{SHOP}/admin/api/2025-07/graphql.json").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"locations": {"nodes": [{"id": "gid://shopify/Location/99", "name": "Main"}]}}},
        )
    )

    params = {"shop": SHOP, "code": "authcode123", "state": state, "timestamp": "1690000000"}
    params["hmac"] = _sign_query(params, API_SECRET)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/shopify/callback", params=params)

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "acme" in resp.text

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("acme")
    assert tenant is not None
    assert tenant.shopify_access_token == "shpat_new_token"
    assert tenant.shopify_location_id == "gid://shopify/Location/99"
    assert tenant.active is False


@respx.mock
async def test_callback_invalid_hmac_rejected(app_ctx):
    app, _settings, _ = app_ctx
    state = await _get_state(app, SHOP)

    params = {"shop": SHOP, "code": "authcode123", "state": state, "timestamp": "1690000000"}
    params["hmac"] = "0" * 64

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/shopify/callback", params=params)

    assert resp.status_code == 401


@respx.mock
async def test_callback_state_mismatch_rejected(app_ctx):
    app, _settings, _ = app_ctx
    await _get_state(app, SHOP)  # a valid state exists, but we won't use it

    params = {"shop": SHOP, "code": "authcode123", "state": "totally-wrong-state", "timestamp": "1690000000"}
    params["hmac"] = _sign_query(params, API_SECRET)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/shopify/callback", params=params)

    assert resp.status_code == 401


@respx.mock
async def test_callback_state_cannot_be_replayed(app_ctx):
    app, _settings, _ = app_ctx
    state = await _get_state(app, SHOP)

    respx.post(f"https://{SHOP}/admin/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "shpat_new_token"})
    )
    respx.post(f"https://{SHOP}/admin/api/2025-07/graphql.json").mock(
        return_value=httpx.Response(200, json={"data": {"locations": {"nodes": []}}})
    )

    params = {"shop": SHOP, "code": "authcode123", "state": state, "timestamp": "1690000000"}
    params["hmac"] = _sign_query(params, API_SECRET)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/shopify/callback", params=params)
        second = await client.get("/shopify/callback", params=params)

    assert first.status_code == 200
    assert second.status_code == 401
