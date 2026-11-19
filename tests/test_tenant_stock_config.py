"""Tenant API: the stock rule and its warehouse scope are validated at write time
(HTTP 422) instead of exploding later inside a background pull, and are exposed
on ``TenantOut`` so the UI can render the current state."""
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
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import tenants
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import Tenant

ADMIN = {"Authorization": "Bearer test-admin-token"}


def _tenant(slug: str, **overrides) -> Tenant:
    base = dict(
        id=None, slug=slug, bims_base_url="https://bims.example.com", bims_api_key="k",
        shopify_shop_domain=f"{slug}.myshopify.com", shopify_access_token="t",
        shopify_webhook_secret="w", shopify_location_id="gid://shopify/Location/1",
        bims_posale_id=1, bims_warehouse_id=1, bims_company_id=1, bims_currency_id=1,
        bims_payment_method_id=1, default_customer_contact_id=1,
    )
    base.update(overrides)
    return Tenant(**base)


@pytest.fixture
async def app():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="test-admin-token",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from alembic.config import Config

    from alembic import command

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
    app.include_router(tenants.router)
    async with factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        await repo.create(_tenant("acme"))
        await repo.create(
            _tenant(
                "scoped",
                field_mappings={
                    "stock_figure_rule": "warehouse_scope",
                    "stock_scope_warehouse_ids": [27],
                },
            )
        )
        await repo.create(_tenant("legacy", field_mappings={"stock_figure_rule": "bogus"}))
    yield app
    await engine.dispose()
    os.unlink(db_file.name)


async def _call(app, method, path, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.request(method, path, headers=ADMIN, **kwargs)


async def _stored(app, slug):
    async with app.state.session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(app.state.settings.fernet_key))
        return await repo.get_by_slug(slug)


SCOPE = {"stock_figure_rule": "warehouse_scope", "stock_scope_warehouse_ids": [27]}


async def test_patch_accepts_a_valid_scope_and_coerces_nothing_in_storage(app):
    response = await _call(app, "PATCH", "/tenants/acme", json={"field_mappings": SCOPE})

    assert response.status_code == 200
    assert (await _stored(app, "acme")).field_mappings == SCOPE


@pytest.mark.parametrize(
    "mappings",
    [
        {"stock_figure_rule": "warehouse-scope"},  # typo in the rule
        {"stock_figure_rule": "warehouse_scope", "stock_scope_warehouse_ids": ["abc"]},
        {"stock_figure_rule": "detail_only", "stock_scope_warehouse_ids": "27"},
        {"stock_figure_rule": "warehouse_scope", "stock_scope_warehouse_ids": [0]},
        {"stock_figure_rule": "warehouse_scope"},  # scope rule without any scope
        {"stock_figure_rule": "warehouse_scope", "stock_scope_warehouse_ids": []},
    ],
)
async def test_patch_rejects_invalid_stock_config_with_422_and_stores_nothing(app, mappings):
    response = await _call(app, "PATCH", "/tenants/acme", json={"field_mappings": mappings})

    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "field_mappings"
    assert (await _stored(app, "acme")).field_mappings == {}


async def test_put_rejects_invalid_stock_config_with_422(app):
    body = {
        "slug": "acme", "bims_base_url": "https://bims.example.com", "bims_api_key": "k",
        "shopify_shop_domain": "acme.myshopify.com", "shopify_access_token": "t",
        "shopify_webhook_secret": "w",
        "field_mappings": {"stock_figure_rule": "nope"},
    }

    assert (await _call(app, "PUT", "/tenants/acme", json=body)).status_code == 422
    body["field_mappings"] = SCOPE
    assert (await _call(app, "PUT", "/tenants/acme", json=body)).status_code == 200


async def test_post_rejects_invalid_stock_config_with_422(app):
    body = {
        "slug": "fresh", "bims_base_url": "https://bims.example.com", "bims_api_key": "k",
        "shopify_shop_domain": "fresh.myshopify.com", "shopify_access_token": "t",
        "shopify_webhook_secret": "w",
        "field_mappings": {"stock_scope_warehouse_ids": [True]},
    }

    assert (await _call(app, "POST", "/tenants", json=body)).status_code == 422
    assert await _stored(app, "fresh") is None


async def test_patch_that_does_not_touch_field_mappings_is_not_blocked_by_a_bad_stored_value(app):
    """A tenant already holding a bad value can still be edited for unrelated fields."""
    response = await _call(app, "PATCH", "/tenants/legacy", json={"reorder_threshold": 2.0})

    assert response.status_code == 200


async def test_tenant_out_exposes_the_effective_rule_and_scope(app):
    default = (await _call(app, "GET", "/tenants/acme")).json()
    scoped = (await _call(app, "GET", "/tenants/scoped")).json()

    assert default["stock_figure_rule"] == "detail_or_total"
    assert default["stock_scope_warehouse_ids"] == []
    assert scoped["stock_figure_rule"] == "warehouse_scope"
    assert scoped["stock_scope_warehouse_ids"] == [27]


async def test_tenant_out_never_500s_on_a_bad_stored_value(app):
    response = await _call(app, "GET", "/tenants/legacy")

    assert response.status_code == 200
    assert response.json()["stock_figure_rule"] == "bogus"
    assert response.json()["stock_scope_warehouse_ids"] == []
