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
