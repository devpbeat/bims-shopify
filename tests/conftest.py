"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        id=1,
        slug="acme",
        bims_base_url="https://bims.example.com",
        bims_api_key="acme_secretkey123",
        shopify_shop_domain="acme.myshopify.com",
        shopify_access_token="shpat_test",
        shopify_webhook_secret="whsecret",
        shopify_location_id="gid://shopify/Location/1",
        bims_posale_id=1,
        bims_warehouse_id=1,
        bims_company_id=1,
        bims_currency_id=1,
        bims_payment_method_id=1,
        default_customer_contact_id=1,
        reorder_threshold=5.0,
        reorder_strategy=ReorderStrategy.PURCHASE_ORDER,
        payment_provider=PaymentProvider.BANCARD,
        provider_config={},
        field_mappings={},
        active=True,
    )


@pytest.fixture
def sequential_paging(monkeypatch):
    """Force the catalog pager to one request at a time.

    For tests whose respx mocks script responses by CALL ORDER: windowed paging
    legitimately issues speculative offsets, which an ordered script can't serve.
    """
    monkeypatch.setattr("bims_shopify.adapters.bims.paging.DEFAULT_PAGE_CONCURRENCY", 1)


@pytest.fixture
async def migrated_db():
    """A real, fully migrated temp SQLite database.

    Yields ``(session_factory, make_tenant)``. Migrations run for real (not
    ``create_all``) so tests see exactly the production schema.
    """
    import asyncio
    import os
    import tempfile
    from pathlib import Path

    from alembic.config import Config
    from cryptography.fernet import Fernet
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command
    from bims_shopify.adapters.persistence.crypto import SecretBox
    from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    database_url = f"sqlite+aiosqlite:///{db_file.name}"
    secret_box = SecretBox(Fernet.generate_key().decode("utf-8"))
    engine = create_async_engine(database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    async def make_tenant(**overrides) -> Tenant:
        defaults = dict(
            id=None,
            slug="acme",
            bims_base_url="https://bims.example.com",
            bims_api_key="acme_secretkey",
            shopify_shop_domain="acme.myshopify.com",
            shopify_access_token="shpat_test",
            shopify_webhook_secret="whsecret",
            shopify_location_id="gid://shopify/Location/1",
            bims_posale_id=1,
            bims_warehouse_id=1,
            bims_company_id=1,
            bims_currency_id=1,
            bims_payment_method_id=1,
            default_customer_contact_id=1,
        )
        defaults.update(overrides)
        async with session_factory() as session:
            return await SqlAlchemyTenantRepository(session, secret_box).create(Tenant(**defaults))

    yield session_factory, make_tenant
    await engine.dispose()
    os.unlink(db_file.name)


VPOS_PRIVATE_KEY = "vpos-private-key"


@pytest.fixture
async def vpos_app():
    """A FastAPI app over a migrated temp DB plus a vPOS tenant factory.

    Yields ``(app, settings, make_vpos_tenant)``.
    """
    import asyncio
    import os
    import tempfile
    from pathlib import Path

    from alembic.config import Config
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command
    from bims_shopify.adapters.persistence.crypto import SecretBox
    from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
    from bims_shopify.api import pay, payments
    from bims_shopify.config import Settings

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="test-admin-token",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
        public_base_url="https://pay.example.com",
    )
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
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
    app.include_router(payments.router)
    app.include_router(pay.router)

    async def make_vpos_tenant(**overrides) -> Tenant:
        defaults = dict(
            id=None,
            slug="acme",
            bims_base_url="https://bims.example.com",
            bims_api_key="acme_secretkey",
            shopify_shop_domain="acme.myshopify.com",
            shopify_access_token="shpat_test",
            shopify_webhook_secret="whsecret",
            shopify_location_id="gid://shopify/Location/1",
            bims_posale_id=1,
            bims_warehouse_id=1,
            bims_company_id=1,
            bims_currency_id=1,
            bims_payment_method_id=1,
            default_customer_contact_id=1,
            payment_provider=PaymentProvider.BANCARD_VPOS,
            provider_config={"public_key": "vpos-public", "environment": "staging"},
            bancard_vpos_private_key=VPOS_PRIVATE_KEY,
        )
        defaults.update(overrides)
        async with session_factory() as session:
            repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
            return await repo.create(Tenant(**defaults))

    yield app, settings, make_vpos_tenant
    await engine.dispose()
    os.unlink(db_file.name)
