"""Bancard vPOS private key: encrypted at rest, nullable, never in provider_config."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import TenantModel
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.domain.tenant import PaymentProvider, Tenant

PRIVATE_KEY = "abc-vpos-private-key"


@pytest.fixture
async def ctx():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    url = f"sqlite+aiosqlite:///{db_file.name}"
    engine = create_async_engine(url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)
    box = SecretBox(Fernet.generate_key().decode("utf-8"))
    yield factory, box
    await engine.dispose()
    os.unlink(db_file.name)


def _tenant(**overrides) -> Tenant:
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
    return Tenant(**defaults)


async def _raw_column(factory, slug: str):
    async with factory() as session:
        row = await session.execute(
            sa.select(TenantModel.bancard_vpos_private_key_encrypted).where(
                TenantModel.slug == slug
            )
        )
        return row.scalar_one()


def test_enum_has_vpos_and_keeps_legacy_bancard():
    assert PaymentProvider("bancard_vpos") is PaymentProvider.BANCARD_VPOS
    assert PaymentProvider.BANCARD.value == "bancard"


async def test_private_key_is_encrypted_at_rest_and_decrypted_on_load(ctx):
    factory, box = ctx
    async with factory() as session:
        repo = SqlAlchemyTenantRepository(session, box)
        await repo.create(
            _tenant(
                payment_provider=PaymentProvider.BANCARD_VPOS,
                bancard_vpos_private_key=PRIVATE_KEY,
            )
        )
    raw = await _raw_column(factory, "acme")
    assert raw is not None and raw != PRIVATE_KEY
    assert box.decrypt(raw) == PRIVATE_KEY
    async with factory() as session:
        loaded = await SqlAlchemyTenantRepository(session, box).get_by_slug("acme")
    assert loaded.bancard_vpos_private_key == PRIVATE_KEY


async def test_unset_key_round_trips_as_none_and_stores_null(ctx):
    factory, box = ctx
    async with factory() as session:
        await SqlAlchemyTenantRepository(session, box).create(_tenant())
    assert await _raw_column(factory, "acme") is None
    async with factory() as session:
        loaded = await SqlAlchemyTenantRepository(session, box).get_by_slug("acme")
    assert loaded.bancard_vpos_private_key is None


async def test_clearing_key_to_none_stores_null(ctx):
    factory, box = ctx
    async with factory() as session:
        repo = SqlAlchemyTenantRepository(session, box)
        created = await repo.create(_tenant(bancard_vpos_private_key=PRIVATE_KEY))
        created.bancard_vpos_private_key = None
        await repo.update(created)
    assert await _raw_column(factory, "acme") is None


async def test_provider_config_never_carries_the_secret(ctx):
    factory, box = ctx
    async with factory() as session:
        await SqlAlchemyTenantRepository(session, box).create(
            _tenant(
                payment_provider=PaymentProvider.BANCARD_VPOS,
                provider_config={"public_key": "pub", "environment": "staging"},
                bancard_vpos_private_key=PRIVATE_KEY,
            )
        )
    async with factory() as session:
        stored = (
            await session.execute(
                sa.select(TenantModel.provider_config).where(TenantModel.slug == "acme")
            )
        ).scalar_one()
    assert "private_key" not in stored
    assert PRIVATE_KEY not in json.dumps(stored)
