"""orders/create payment-link bridge: provider selection and pinned idempotency keys."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.api import webhooks
from bims_shopify.domain.tenant import PaymentProvider

SINGLE_BUY = "https://vpos.infonet.com.py:8888/vpos/api/0.3/single_buy"
PAGOPAR_INIT = "https://api.pagopar.com/api/comercios/2.0/iniciar-transaccion"
SECRET = "whsecret"


@pytest.fixture
def bridge_app(vpos_app):
    app, settings, make_tenant = vpos_app
    app.include_router(webhooks.router)

    async def make_bridge_tenant(**overrides):
        return await make_tenant(**{"push_orders_to_bims": False, **overrides})

    return app, settings, make_bridge_tenant


def _payload(order_id=1001, gateway="Bancard", total="130.00"):
    return {"id": order_id, "total_price": total, "currency": "PYG", "payment_gateway_names": [gateway]}


async def _post_order(app, payload) -> httpx.Response:
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/webhooks/shopify/acme",
            content=body,
            headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "orders/create"},
        )


@pytest.fixture
def claimed_sources(monkeypatch):
    sources: list[str] = []
    original = SqlAlchemySyncStateRepository.try_claim_event

    async def spy(self, tenant_id, source, external_id, payload_hash):
        sources.append(source)
        return await original(self, tenant_id, source, external_id, payload_hash)

    monkeypatch.setattr(SqlAlchemySyncStateRepository, "try_claim_event", spy)
    return sources


@respx.mock
async def test_pagopar_claim_source_is_the_pinned_literal(bridge_app, claimed_sources):
    app, _, make_tenant = bridge_app
    await make_tenant(
        payment_provider=PaymentProvider.PAGOPAR,
        provider_config={"public_key": "pub", "private_key": "priv"},
        bancard_vpos_private_key=None,
    )
    respx.post(PAGOPAR_INIT).mock(
        return_value=httpx.Response(200, json={"respuesta": True, "resultado": [{"data": "h-1"}]})
    )

    resp = await _post_order(app, _payload(gateway="Pagopar"))

    assert resp.status_code == 200
    # Literal on purpose: renaming provider.name must never orphan in-flight claims.
    assert "payment_link:pagopar" in claimed_sources


@respx.mock
async def test_vpos_tenant_gets_a_signed_link_and_provider_scoped_source(bridge_app, claimed_sources):
    app, settings, make_tenant = bridge_app
    tenant = await make_tenant()
    route = respx.post(SINGLE_BUY).mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "pid-1"})
    )

    resp = await _post_order(app, _payload())

    assert resp.status_code == 200
    assert route.call_count == 1
    assert "payment_link:bancard_vpos" in claimed_sources
    async with app.state.session_factory() as session:
        intent = await SqlAlchemyPaymentIntentRepository(session).get_by_order_id(
            tenant.id, "bancard_vpos", "1001"
        )
    assert intent is not None and intent.status == "pending"
    assert intent.checkout_url.startswith(f"{settings.public_base_url}/pay/bancard_vpos/acme/")


@respx.mock
async def test_vpos_ignores_orders_paid_with_another_gateway(bridge_app):
    app, _, make_tenant = bridge_app
    await make_tenant()
    route = respx.post(SINGLE_BUY).mock(return_value=httpx.Response(200, json={}))

    resp = await _post_order(app, _payload(gateway="Manual"))

    assert resp.status_code == 200 and not route.called


@respx.mock
async def test_legacy_bancard_tenant_gets_no_link(bridge_app):
    app, _, make_tenant = bridge_app
    await make_tenant(payment_provider=PaymentProvider.BANCARD, provider_config={})
    route = respx.post(SINGLE_BUY).mock(return_value=httpx.Response(200, json={}))

    resp = await _post_order(app, _payload())

    assert resp.status_code == 200 and not route.called


@respx.mock
@pytest.mark.parametrize("order_id", ["gid-not-numeric", 12345678901234])
async def test_vpos_unusable_order_id_makes_no_bancard_call_and_still_succeeds(bridge_app, order_id):
    app, _, make_tenant = bridge_app
    await make_tenant()
    route = respx.post(SINGLE_BUY).mock(return_value=httpx.Response(200, json={}))

    resp = await _post_order(app, _payload(order_id=order_id))

    assert resp.status_code == 200 and not route.called


@respx.mock
async def test_vpos_without_private_key_makes_no_bancard_call_and_still_succeeds(bridge_app):
    app, _, make_tenant = bridge_app
    await make_tenant(bancard_vpos_private_key=None)
    route = respx.post(SINGLE_BUY).mock(return_value=httpx.Response(200, json={}))

    resp = await _post_order(app, _payload())

    assert resp.status_code == 200 and not route.called
