"""POST /api/portal/{slug}/payments/rollback: auth, tenant isolation, outcomes, audit."""
from __future__ import annotations

import asyncio
import hashlib
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
from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import portal, portal_payment_rollback
from bims_shopify.config import Settings
from bims_shopify.domain.payment import PaymentIntent
from bims_shopify.domain.tenant import PaymentProvider, Tenant

ADMIN = {"Authorization": "Bearer test-admin-token"}
SECRET = "super-secret-private-key"
ROLLBACK = "https://vpos.infonet.com.py:8888/vpos/api/0.3/single_buy/rollback"
CFG = {"public_key": "pub", "environment": "staging", "gateway_name": "Bancard"}


def _tenant(slug: str, provider: PaymentProvider, key: str | None) -> Tenant:
    return Tenant(
        id=None, slug=slug, bims_base_url="https://bims.example.com", bims_api_key="k",
        shopify_shop_domain=f"{slug}.myshopify.com", shopify_access_token="t",
        shopify_webhook_secret="w", shopify_location_id="gid://shopify/Location/1",
        bims_posale_id=1, bims_warehouse_id=1, bims_company_id=1, bims_currency_id=1,
        bims_payment_method_id=1, default_customer_contact_id=1,
        payment_provider=provider, provider_config=dict(CFG), bancard_vpos_private_key=key,
    )


async def _seed(factory, tenant_id, order, spid, status="paid"):
    async with factory() as session:
        await SqlAlchemyPaymentIntentRepository(session).save(
            tenant_id, "bancard_vpos",
            PaymentIntent(
                tenant_slug="", order_id=order, amount=130.0, currency="PYG",
                provider_reference=spid, status=status,
            ),
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
    app.include_router(portal_payment_rollback.router)
    async with factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        acme = await repo.create(_tenant("acme", PaymentProvider.BANCARD_VPOS, SECRET))
        other = await repo.create(_tenant("other", PaymentProvider.BANCARD_VPOS, "other-key"))
        await repo.set_portal_token_hash(other.id, hashlib.sha256(b"other-token").hexdigest())
        await repo.create(_tenant("pago", PaymentProvider.PAGOPAR, None))
    await _seed(factory, acme.id, "900", "90000")
    await _seed(factory, acme.id, "901", None, status="pending")
    await _seed(factory, other.id, "700", "70000")
    yield app
    await engine.dispose()
    os.unlink(db_file.name)


async def _post(app, slug, body, headers=ADMIN):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/api/portal/{slug}/payments/rollback", headers=headers, json=body)


async def _status(app, tenant_slug, order):
    async with app.state.session_factory() as session:
        tenant = await SqlAlchemyTenantRepository(
            session, SecretBox(app.state.settings.fernet_key)
        ).get_by_slug(tenant_slug)
        intent = await SqlAlchemyPaymentIntentRepository(session).get_by_order_id(
            tenant.id, "bancard_vpos", order
        )
        return intent.status


async def _audit(app, action="payment.rollback_requested"):
    async with app.state.session_factory() as session:
        rows = await session.scalars(select(AuditLogModel).where(AuditLogModel.action == action))
        return list(rows)


def _reply(key, status="error"):
    return httpx.Response(
        200, json={"status": status, "messages": [{"key": key, "level": "info", "dsc": "d"}]}
    )


async def test_rollback_requires_portal_authentication(app):
    assert (await _post(app, "acme", {"order_id": "900"}, headers={})).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await _post(app, "acme", {"order_id": "900"}, headers=bad)).status_code == 401


@respx.mock
async def test_success_reverses_cancels_locally_and_audits(app):
    route = respx.post(ROLLBACK).mock(return_value=_reply("RollbackSuccessful", "success"))

    response = await _post(app, "acme", {"order_id": "900"})

    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "succeeded" and body["key"] == "RollbackSuccessful"
    assert body["order_id"] == "900" and body["shop_process_id"] == "90000"
    assert body["status_before"] == "paid" and body["local_status"] == "cancelled"
    assert body["manual_action_required"] is False
    assert body["message"] and body["message_es"]
    assert SECRET not in response.text and SECRET not in route.calls.last.request.content.decode()
    assert await _status(app, "acme", "900") == "cancelled"
    (entry,) = await _audit(app)
    assert entry.payload["outcome"] == "succeeded" and entry.actor == "portal"
    assert SECRET not in str(entry.payload)


@respx.mock
async def test_lookup_by_shop_process_id_and_idempotent_second_call(app):
    respx.post(ROLLBACK).mock(
        side_effect=[_reply("RollbackSuccessful", "success"), _reply("AlreadyRollbackedError")]
    )

    first = await _post(app, "acme", {"shop_process_id": "90000"})
    second = await _post(app, "acme", {"shop_process_id": "90000"})

    assert first.json()["outcome"] == "succeeded"
    assert second.status_code == 200 and second.json()["outcome"] == "no_op"
    assert second.json()["local_status"] == "cancelled"
    assert await _status(app, "acme", "900") == "cancelled"


@respx.mock
async def test_manual_annulment_is_a_loud_409_and_leaves_the_intent_alone(app):
    respx.post(ROLLBACK).mock(return_value=_reply("TransactionAlreadyConfirmed"))

    response = await _post(app, "acme", {"order_id": "900"})

    body = response.json()
    assert response.status_code == 409
    assert body["outcome"] == "manual_annulment_required" and body["manual_action_required"] is True
    assert await _status(app, "acme", "900") == "paid"
    assert len(await _audit(app, "payment.rollback_conflict")) == 1


@respx.mock
@pytest.mark.parametrize("reply", [httpx.Response(500, text="boom"), _reply("PosCommunicationError")])
async def test_transport_or_unexpected_outcomes_are_502(app, reply):
    respx.post(ROLLBACK).mock(return_value=reply)

    response = await _post(app, "acme", {"order_id": "900"})

    assert response.status_code == 502 and response.json()["outcome"] in {"transport_failure", "unexpected"}
    assert await _status(app, "acme", "900") == "paid"


@respx.mock
async def test_rejected_credentials_are_409_not_a_payment_outcome(app):
    respx.post(ROLLBACK).mock(return_value=_reply("InvalidTokenError"))

    response = await _post(app, "acme", {"order_id": "900"})

    assert response.status_code == 409 and response.json()["detail"]["code"] == "vpos_configuration"
    assert SECRET not in response.text


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "missing_identifier"),
        ({"order_id": " ", "shop_process_id": ""}, "missing_identifier"),
        ({"order_id": "900", "shop_process_id": "99999"}, "ambiguous"),
        ({"shop_process_id": "abc"}, "invalid_shop_process_id"),
    ],
)
async def test_bad_input_is_422_and_never_reaches_bancard(app, body, code):
    with respx.mock:
        response = await _post(app, "acme", body)
        assert not respx.calls
    assert response.status_code == 422 and response.json()["detail"]["code"] == code
    assert response.json()["detail"]["message"] and response.json()["detail"]["message_es"]


async def test_unknown_payment_is_404(app):
    response = await _post(app, "acme", {"order_id": "404"})
    assert response.status_code == 404 and response.json()["detail"]["code"] == "payment_not_found"


async def test_placeholder_without_an_operation_is_409(app):
    response = await _post(app, "acme", {"order_id": "901"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "no_operation"


async def test_a_tenant_can_never_roll_back_another_tenants_payment(app):
    own = {"Authorization": "Bearer other-token"}
    with respx.mock:
        cross_slug = await _post(app, "acme", {"order_id": "900"}, headers=own)
        cross_order = await _post(app, "other", {"order_id": "900"}, headers=own)
        cross_ref = await _post(app, "other", {"shop_process_id": "90000"}, headers=own)
        assert not respx.calls
    assert cross_slug.status_code == 401
    assert cross_order.status_code == 404 and cross_ref.status_code == 404
    assert await _status(app, "acme", "900") == "paid"


async def test_tenant_without_vpos_is_409(app):
    response = await _post(app, "pago", {"order_id": "900"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "provider_not_supported"


def test_the_rollback_route_is_registered_on_the_real_app():
    from bims_shopify import main

    paths = main.create_app().openapi()["paths"]
    assert "post" in paths["/api/portal/{slug}/payments/rollback"]
