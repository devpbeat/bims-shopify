"""Portal payments-config API: read, write-only private key, audit, test-connection."""
from __future__ import annotations

import asyncio
import hashlib
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import AuditLogModel
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import portal, portal_payments
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import PROBE_SHOP_PROCESS_ID
from bims_shopify.domain.tenant import PaymentProvider, Tenant

ADMIN = {"Authorization": "Bearer test-admin-token"}
SECRET = "super-secret-private-key"
STAGING = "https://vpos.infonet.com.py:8888"
CONFIRMATIONS = f"{STAGING}/vpos/api/0.3/single_buy/confirmations"


def _tenant(slug: str, provider: PaymentProvider | None, key: str | None, config: dict) -> Tenant:
    return Tenant(
        id=None, slug=slug, bims_base_url="https://bims.example.com", bims_api_key="k",
        shopify_shop_domain=f"{slug}.myshopify.com", shopify_access_token="t",
        shopify_webhook_secret="w", shopify_location_id="gid://shopify/Location/1",
        bims_posale_id=1, bims_warehouse_id=1, bims_company_id=1, bims_currency_id=1,
        bims_payment_method_id=1, default_customer_contact_id=1,
        payment_provider=provider, provider_config=config, bancard_vpos_private_key=key,
    )


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
    app.include_router(portal.router)
    app.include_router(portal_payments.router)
    cfg_ok = {"public_key": "pub", "environment": "staging", "gateway_name": "Bancard"}
    async with factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        await repo.create(_tenant("acme", PaymentProvider.BANCARD_VPOS, SECRET, dict(cfg_ok)))
        await repo.create(_tenant("nokey", PaymentProvider.BANCARD_VPOS, None, dict(cfg_ok)))
        await repo.create(_tenant("fresh", None, None, {}))
        await repo.create(_tenant("pago", PaymentProvider.PAGOPAR, None, {}))
        other = await repo.create(_tenant("other", PaymentProvider.BANCARD_VPOS, "other-key", dict(cfg_ok)))
        await repo.set_portal_token_hash(other.id, hashlib.sha256(b"other-token").hexdigest())
    yield app
    await engine.dispose()
    os.unlink(db_file.name)


async def _call(app, method, path, *, headers=ADMIN, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, f"/api/portal/{path}", headers=headers, **kwargs)


async def _load(app, slug):
    async with app.state.session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(app.state.settings.fernet_key))
        return await repo.get_by_slug(slug)


async def _audit(app, action):
    async with app.state.session_factory() as session:
        rows = await session.scalars(select(AuditLogModel).where(AuditLogModel.action == action))
        return list(rows)


async def test_get_exposes_flag_and_confirm_url_never_the_key(app):
    response = await _call(app, "GET", "acme/payments-config")
    body = response.json()
    assert body["private_key_configured"] is True
    assert body["public_key"] == "pub" and body["environment"] == "staging"
    assert body["provider"] == "bancard_vpos" and body["editable"] is True
    assert body["confirm_url"] == (
        "https://mystoresync.ignitesolutions.click/payments/bancard_vpos/acme/callback"
    )
    assert SECRET not in response.text
    assert (await _call(app, "GET", "nokey/payments-config")).json()["private_key_configured"] is False


async def test_get_marks_pagopar_tenant_not_editable(app):
    body = (await _call(app, "GET", "pago/payments-config")).json()
    assert body["editable"] is False and body["private_key_configured"] is False


@pytest.mark.parametrize("payload", [{}, {"private_key": ""}, {"private_key": None}, {"private_key": "  "}])
async def test_put_without_private_key_leaves_it_unchanged(app, payload):
    response = await _call(app, "PUT", "acme/payments-config", json={"public_key": "pub2", **payload})
    assert response.status_code == 200
    assert response.json()["private_key_configured"] is True
    tenant = await _load(app, "acme")
    assert tenant.bancard_vpos_private_key == SECRET
    assert tenant.provider_config["public_key"] == "pub2"
    assert tenant.provider_config["environment"] == "staging"


async def test_put_replaces_key_and_audits_field_names_only(app):
    response = await _call(
        app, "PUT", "acme/payments-config",
        json={"private_key": "new-secret", "environment": "production"},
    )
    assert response.status_code == 200 and "new-secret" not in response.text
    assert (await _load(app, "acme")).bancard_vpos_private_key == "new-secret"
    (entry,) = await _audit(app, "payments.config_updated")
    assert sorted(entry.payload["fields"]) == ["environment", "private_key"]
    assert "new-secret" not in json.dumps(entry.payload)


@pytest.mark.parametrize(
    "slug,payload",
    [
        ("acme", {"environment": "sandbox"}),
        ("acme", {"provider": "pagopar"}),
        ("fresh", {"provider": "bancard_vpos", "public_key": "p"}),
    ],
)
async def test_put_rejects_invalid_input(app, slug, payload):
    assert (await _call(app, "PUT", f"{slug}/payments-config", json=payload)).status_code == 422


async def test_put_activates_vpos_on_unconfigured_tenant_when_key_given(app):
    body = {"public_key": "p", "private_key": "k1", "environment": "staging"}
    assert (await _call(app, "PUT", "fresh/payments-config", json=body)).status_code == 200
    tenant = await _load(app, "fresh")
    assert tenant.payment_provider is PaymentProvider.BANCARD_VPOS


async def test_put_refuses_to_override_other_provider(app):
    response = await _call(app, "PUT", "pago/payments-config", json={"public_key": "p"})
    assert response.status_code == 409
    assert (await _load(app, "pago")).payment_provider is PaymentProvider.PAGOPAR


async def test_requires_authentication_and_blocks_cross_tenant(app):
    assert (await _call(app, "GET", "acme/payments-config", headers={})).status_code == 401
    other = {"Authorization": "Bearer other-token"}
    response = await _call(app, "PUT", "acme/payments-config", headers=other, json={"public_key": "evil"})
    assert response.status_code == 401
    assert (await _load(app, "acme")).provider_config["public_key"] == "pub"
    assert (await _call(app, "GET", "other/payments-config", headers=other)).status_code == 200


@respx.mock
async def test_test_connection_without_key_makes_no_http_call(app):
    response = await _call(app, "POST", "nokey/payments-config/test-connection")
    assert response.json()["ok"] is False and response.json()["reason"] == "not_configured"
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize("key", ["BuyNotFoundError", "PaymentNotFoundError"])
async def test_test_connection_ok_on_signed_not_found_and_only_probes(app, key):
    route = respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(200, json={"status": "error", "messages": [{"key": key, "level": "error", "dsc": "x"}]})
    )
    response = await _call(app, "POST", "acme/payments-config/test-connection")
    assert response.json()["ok"] is True
    assert len(respx.calls) == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["operation"]["shop_process_id"] == PROBE_SHOP_PROCESS_ID
    assert SECRET not in response.text
    (entry,) = await _audit(app, "payments.config_tested")
    assert entry.payload["ok"] is True and SECRET not in json.dumps(entry.payload)


@respx.mock
@pytest.mark.parametrize(
    "key,reason",
    [
        ("InvalidTokenError", "invalid_credentials"),
        ("InvalidPublicKeyError", "invalid_credentials"),
        ("ApplicationNotFoundError", "application_not_enabled"),
        ("UnauthorizedOperationError", "application_not_enabled"),
        ("SomethingNew", "unexpected"),
    ],
)
async def test_test_connection_maps_credential_failures(app, key, reason):
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(200, json={"status": "error", "messages": [{"key": key, "level": "error", "dsc": "x"}]})
    )
    body = (await _call(app, "POST", "acme/payments-config/test-connection")).json()
    assert body["ok"] is False and body["reason"] == reason and SECRET not in body["message"]


@respx.mock
@pytest.mark.parametrize("effect", [httpx.ConnectError("boom"), httpx.Response(503)])
async def test_test_connection_reports_unreachable(app, effect):
    respx.post(CONFIRMATIONS).mock(side_effect=effect) if isinstance(effect, Exception) else respx.post(
        CONFIRMATIONS
    ).mock(return_value=effect)
    body = (await _call(app, "POST", "acme/payments-config/test-connection")).json()
    assert body["ok"] is False and body["reason"] == "unreachable"
