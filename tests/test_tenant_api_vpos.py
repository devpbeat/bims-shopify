"""Tenant admin API: the vPOS private key is write-only and the environment is validated."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import tenants
from bims_shopify.config import Settings

ADMIN = {"Authorization": "Bearer test-admin-token"}
SECRET = "vpos-private-key-value"


def _body(**overrides) -> dict:
    body = dict(
        slug="acme",
        bims_base_url="https://bims.example.com",
        bims_api_key="acme_key",
        shopify_shop_domain="acme.myshopify.com",
        shopify_access_token="shpat_test",
        shopify_webhook_secret="whsecret",
        payment_provider="bancard_vpos",
        provider_config={"public_key": "pub", "environment": "staging"},
    )
    body.update(overrides)
    return body


@pytest.fixture
async def ctx():
    import asyncio

    from alembic.config import Config

    from alembic import command

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="test-admin-token",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = settings.database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = factory
    app.state.engine = engine
    app.include_router(tenants.router)
    yield app, factory, settings
    await engine.dispose()
    os.unlink(db_file.name)


async def _stored_key(factory, settings) -> str | None:
    async with factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        return (await repo.get_by_slug("acme")).bancard_vpos_private_key


async def test_create_accepts_the_key_and_never_echoes_it(ctx):
    app, factory, settings = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        created = await client.post(
            "/tenants", headers=ADMIN, json=_body(bancard_vpos_private_key=SECRET)
        )
        fetched = await client.get("/tenants/acme", headers=ADMIN)
        listed = await client.get("/tenants", headers=ADMIN)
    assert created.status_code == 201
    assert created.json()["bancard_vpos_private_key_configured"] is True
    for response in (created, fetched, listed):
        assert SECRET not in response.text
        assert "bancard_vpos_private_key\"" not in response.text
    assert await _stored_key(factory, settings) == SECRET


async def test_flag_is_false_when_no_key_is_set(ctx):
    app, _factory, _settings = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        created = await client.post("/tenants", headers=ADMIN, json=_body())
    assert created.json()["bancard_vpos_private_key_configured"] is False


async def test_patch_without_key_keeps_it_and_empty_key_is_rejected(ctx):
    app, factory, settings = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/tenants", headers=ADMIN, json=_body(bancard_vpos_private_key=SECRET))
        kept = await client.patch("/tenants/acme", headers=ADMIN, json={"active": False})
        empty = await client.patch(
            "/tenants/acme", headers=ADMIN, json={"bancard_vpos_private_key": ""}
        )
        rotated = await client.patch(
            "/tenants/acme", headers=ADMIN, json={"bancard_vpos_private_key": "rotated"}
        )
    assert kept.status_code == 200
    assert empty.status_code == 422
    assert rotated.status_code == 200
    assert await _stored_key(factory, settings) == "rotated"


async def test_put_without_key_does_not_erase_the_stored_key(ctx):
    app, factory, settings = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/tenants", headers=ADMIN, json=_body(bancard_vpos_private_key=SECRET))
        replaced = await client.put("/tenants/acme", headers=ADMIN, json=_body())
    assert replaced.status_code == 200
    assert replaced.json()["bancard_vpos_private_key_configured"] is True
    assert await _stored_key(factory, settings) == SECRET


async def test_patch_audit_lists_the_field_name_only(ctx):
    from sqlalchemy import select

    from bims_shopify.adapters.persistence.models import AuditLogModel

    app, factory, _settings = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/tenants", headers=ADMIN, json=_body())
        await client.patch(
            "/tenants/acme", headers=ADMIN, json={"bancard_vpos_private_key": SECRET}
        )
    async with factory() as session:
        rows = (await session.execute(select(AuditLogModel))).scalars().all()
    dump = " ".join(str(row.payload) for row in rows)
    assert "bancard_vpos_private_key" in dump
    assert SECRET not in dump


@pytest.mark.parametrize("method", ["post", "put", "patch"])
async def test_invalid_environment_is_rejected_with_422(ctx, method):
    app, _factory, _settings = ctx
    bad = {"public_key": "pub", "environment": "sandbox"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        if method == "post":
            response = await client.post("/tenants", headers=ADMIN, json=_body(provider_config=bad))
        else:
            await client.post("/tenants", headers=ADMIN, json=_body())
            if method == "put":
                response = await client.put(
                    "/tenants/acme", headers=ADMIN, json=_body(provider_config=bad)
                )
            else:
                response = await client.patch(
                    "/tenants/acme", headers=ADMIN, json={"provider_config": bad}
                )
    assert response.status_code == 422


async def test_production_and_staging_environments_are_accepted(ctx):
    app, _factory, _settings = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            "/tenants",
            headers=ADMIN,
            json=_body(provider_config={"public_key": "pub", "environment": "production"}),
        )
    assert response.status_code == 201


async def test_patch_with_null_key_leaves_the_stored_key(ctx):
    app, factory, settings = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/tenants", headers=ADMIN, json=_body(bancard_vpos_private_key=SECRET))
        response = await client.patch(
            "/tenants/acme", headers=ADMIN, json={"bancard_vpos_private_key": None}
        )
    assert response.status_code == 200
    assert await _stored_key(factory, settings) == SECRET
