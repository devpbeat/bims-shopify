"""Tests for automatic tenant onboarding on Shopify OAuth install.

Covers tenant matching (exact domain / slug-derived), BIMS-config-gated
auto-activation, and the dry-run sync kickoff scheduled on activation.
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
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import shopify_oauth, tenants
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import Tenant

API_KEY = "test-client-id"
API_SECRET = "test-client-secret"
BIMS_BASE_URL = "https://bims.mystore.example.com"


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


async def _seed_tenant(session_factory, settings: Settings, **overrides) -> Tenant:
    defaults = dict(
        id=None,
        slug="mystore",
        bims_base_url=BIMS_BASE_URL,
        bims_api_key="mystore_secretkey123",
        shopify_shop_domain="mystore-com-py.myshopify.com",
        shopify_access_token="",
        shopify_webhook_secret="whsecret",
        shopify_location_id="",
        bims_posale_id=1,
        bims_warehouse_id=1,
        bims_company_id=1,
        bims_currency_id=1,
        bims_payment_method_id=1,
        default_customer_contact_id=1,
        active=False,
    )
    defaults.update(overrides)
    tenant = Tenant(**defaults)
    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        return await repo.create(tenant)


async def _get_state(app, shop: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/shopify/install", params={"shop": shop}, follow_redirects=False)
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    return qs["state"][0]


def _mock_shopify_token_and_location(shop: str) -> None:
    respx.post(f"https://{shop}/admin/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "shpat_real_token"})
    )
    respx.post(f"https://{shop}/admin/api/2025-07/graphql.json").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"locations": {"nodes": [{"id": "gid://shopify/Location/1", "name": "Main"}]}}},
        )
    )


async def _run_callback(app, shop: str, state: str):
    params = {"shop": shop, "code": "authcode123", "state": state, "timestamp": "1690000000"}
    params["hmac"] = _sign_query(params, API_SECRET)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/shopify/callback", params=params)


@respx.mock
async def test_match_by_exact_domain_updates_existing_tenant_and_preserves_bims_fields(app_ctx):
    app, settings, session_factory = app_ctx
    shop = "mystore-com-py.myshopify.com"
    await _seed_tenant(session_factory, settings, shopify_shop_domain=shop)

    _mock_shopify_token_and_location(shop)
    respx.get(f"{BIMS_BASE_URL}/api/currencies/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )

    state = await _get_state(app, shop)
    resp = await _run_callback(app, shop, state)
    assert resp.status_code == 200

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("mystore")

    assert tenant is not None
    assert tenant.shopify_shop_domain == shop
    assert tenant.shopify_access_token == "shpat_real_token"
    assert tenant.bims_base_url == BIMS_BASE_URL
    assert tenant.bims_api_key == "mystore_secretkey123"
    assert tenant.active is True


@respx.mock
async def test_match_by_slug_derived_from_shop_subdomain_updates_tenant(app_ctx):
    app, settings, session_factory = app_ctx
    # tenant's stored domain is a wrong guess; the real shop domain differs
    # but its subdomain matches the tenant slug.
    await _seed_tenant(
        session_factory, settings, shopify_shop_domain="mystore-com-py.myshopify.com"
    )

    real_shop = "mystore.myshopify.com"
    _mock_shopify_token_and_location(real_shop)
    respx.get(f"{BIMS_BASE_URL}/api/currencies/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )

    state = await _get_state(app, real_shop)
    resp = await _run_callback(app, real_shop, state)
    assert resp.status_code == 200

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("mystore")

    assert tenant is not None
    assert tenant.shopify_shop_domain == real_shop
    assert tenant.bims_base_url == BIMS_BASE_URL
    assert tenant.active is True


@respx.mock
async def test_no_match_creates_new_tenant_inactive(app_ctx):
    app, settings, session_factory = app_ctx
    shop = "brandnew.myshopify.com"
    _mock_shopify_token_and_location(shop)

    state = await _get_state(app, shop)
    resp = await _run_callback(app, shop, state)
    assert resp.status_code == 200

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("brandnew")

    assert tenant is not None
    assert tenant.active is False


@respx.mock
async def test_bims_validation_success_activates_and_schedules_dry_run_sync(app_ctx):
    app, settings, session_factory = app_ctx
    shop = "mystore.myshopify.com"
    await _seed_tenant(session_factory, settings, shopify_shop_domain="wrong-guess.myshopify.com")

    _mock_shopify_token_and_location(shop)
    respx.get(f"{BIMS_BASE_URL}/api/currencies/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )
    respx.get(f"{BIMS_BASE_URL}/api/products/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )

    state = await _get_state(app, shop)
    resp = await _run_callback(app, shop, state)
    assert resp.status_code == 200

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("mystore")
    assert tenant.active is True

    # background dry-run sync task is scheduled on the event loop; let it run.
    await asyncio.sleep(0.2)

    async with session_factory() as session:
        sync_state_repo = SqlAlchemySyncStateRepository(session)
        status = await sync_state_repo.get_status(tenant.id)
    assert status["last_run_summary"].get("dry_run") is True


@respx.mock
async def test_bims_validation_failure_keeps_tenant_inactive_and_skips_sync(app_ctx):
    app, settings, session_factory = app_ctx
    shop = "mystore.myshopify.com"
    await _seed_tenant(session_factory, settings, shopify_shop_domain="wrong-guess.myshopify.com")

    _mock_shopify_token_and_location(shop)
    respx.get(f"{BIMS_BASE_URL}/api/currencies/index.json").mock(
        return_value=httpx.Response(200, json={"status": "error", "code": "401", "message": "bad key"})
    )

    state = await _get_state(app, shop)
    resp = await _run_callback(app, shop, state)
    assert resp.status_code == 200

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("mystore")
    assert tenant.active is False

    await asyncio.sleep(0.2)

    async with session_factory() as session:
        sync_state_repo = SqlAlchemySyncStateRepository(session)
        status = await sync_state_repo.get_status(tenant.id)
    assert status["last_run_summary"] == {}


@respx.mock
async def test_unknown_edge_case_shop_handled_safely(app_ctx):
    app, settings, session_factory = app_ctx
    # a shop domain with no dot-based subdomain segments beyond the standard
    # pattern must not crash matching logic.
    shop = "a.myshopify.com"
    _mock_shopify_token_and_location(shop)

    state = await _get_state(app, shop)
    resp = await _run_callback(app, shop, state)
    assert resp.status_code == 200

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        tenant = await repo.get_by_slug("a")
    assert tenant is not None
    assert tenant.active is False


@pytest.mark.skipif(os.environ.get("BIMS_LIVE") != "1", reason="opt-in live BIMS check")
async def test_live_bims_validation_against_mystore_tenant(app_ctx):
    """Opt-in: exercises the real BIMS currencies endpoint for tenant 'mystore'.

    Enable with BIMS_LIVE=1 and a dev DB that already has a real 'mystore'
    tenant row with working BIMS credentials. Never runs in CI by default.
    """
    from bims_shopify.adapters.bims.client import BIMSClient
    from bims_shopify.config import get_settings as get_real_settings

    real_settings = get_real_settings()
    async with async_sessionmaker(
        create_async_engine(real_settings.database_url, future=True), expire_on_commit=False
    )() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(real_settings.fernet_key))
        tenant = await repo.get_by_slug("mystore")
    assert tenant is not None
    client = BIMSClient(tenant)
    try:
        body = await client.get("/api/currencies/index.json")
        assert body is not None
    finally:
        await client.aclose()
