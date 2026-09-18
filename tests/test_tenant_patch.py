"""Tests for PATCH /tenants/{slug}: partial updates that never force
callers to resend secrets (bims_api_key, shopify_access_token,
shopify_webhook_secret)."""
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
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import tenants
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

ADMIN_TOKEN = "test-admin-token"


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
        shopify_api_secret="app-client-secret",
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

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        acme = await _make_tenant(repo, "acme")

    yield app, settings, acme
    await engine.dispose()
    os.unlink(db_file.name)


async def _get_model(app: FastAPI, slug: str):
    from sqlalchemy import select

    from bims_shopify.adapters.persistence.models import TenantModel

    async with app.state.session_factory() as session:
        result = await session.execute(select(TenantModel).where(TenantModel.slug == slug))
        return result.scalar_one()


async def test_patch_field_mappings_leaves_secrets_intact(app_ctx):
    app, settings, acme = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/tenants/acme",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"field_mappings": {"sku": "custom_sku"}},
        )
    assert response.status_code == 200
    assert response.json()["slug"] == "acme"

    model = await _get_model(app, "acme")
    secret_box = SecretBox(settings.fernet_key)
    assert secret_box.decrypt(model.bims_api_key_encrypted) == acme.bims_api_key
    assert secret_box.decrypt(model.shopify_access_token_encrypted) == acme.shopify_access_token
    assert model.shopify_webhook_secret == acme.shopify_webhook_secret
    assert model.field_mappings == {"sku": "custom_sku"}


async def test_patch_single_scalar_field(app_ctx):
    app, _settings, _acme = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/tenants/acme",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"active": False},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["active"] is False
    # Untouched fields keep their original values.
    assert body["bims_base_url"] == "https://bims.example.com"


async def test_patch_unknown_tenant_404s(app_ctx):
    app, _settings, _acme = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/tenants/does-not-exist",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"active": False},
        )
    assert response.status_code == 404


async def test_patch_requires_auth(app_ctx):
    app, _settings, _acme = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch("/tenants/acme", json={"active": False})
    assert response.status_code == 401


async def test_patch_rejects_empty_secret(app_ctx):
    app, _settings, _acme = app_ctx
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/tenants/acme",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"bims_api_key": ""},
        )
    assert response.status_code == 422
