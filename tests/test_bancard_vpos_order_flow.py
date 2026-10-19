"""End to end vPOS flow: orders/create -> link -> iframe page -> confirm callback -> order paid."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from bims_shopify.api import webhooks
from bims_shopify.domain.bancard_vpos import VposAmount, build_shop_process_id, confirm_token
from tests.conftest import VPOS_PRIVATE_KEY

STAGING = "https://vpos.infonet.com.py:8888"
SINGLE_BUY = f"{STAGING}/vpos/api/0.3/single_buy"
CONFIRMATIONS = f"{STAGING}/vpos/api/0.3/single_buy/confirmations"
SHOPIFY = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
ORDER_ID = "1001"


def _callback(*, token: str | None = None) -> dict:
    spid = build_shop_process_id(ORDER_ID, 0)
    good = confirm_token(VPOS_PRIVATE_KEY, spid, VposAmount.from_order_total("130.00"))
    return {
        "operation": {
            "token": token or good,
            "shop_process_id": str(spid),
            "response": "S",
            "amount": "130.00",
            "currency": "PYG",
            "authorization_number": "123456",
            "ticket_number": "123456789123456",
            "response_code": "00",
            "iva_amount": "0.0",
        }
    }


def _confirmation(code: str = "00") -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "success",
              "confirmation": {"response_code": code, "amount": "130.00", "currency": "PYG"}},
    )


@pytest.fixture
async def flow(vpos_app):
    app, settings, make_tenant = vpos_app
    app.include_router(webhooks.router)
    await make_tenant(push_orders_to_bims=False)
    return app, settings


async def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


async def _place_order(app) -> None:
    body = json.dumps({"id": int(ORDER_ID), "total_price": "130.00", "currency": "PYG",
                       "payment_gateway_names": ["Bancard"]}).encode()
    sig = base64.b64encode(hmac.new(b"whsecret", body, hashlib.sha256).digest()).decode()
    resp = await _request(app, "POST", "/webhooks/shopify/acme", content=body,
                          headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "orders/create"})
    assert resp.status_code == 200


async def _checkout_path(app) -> str:
    resp = await _request(app, "GET", "/api/portal/acme/payments")
    return resp.json()


def _mock_happy_path(code: str = "00"):
    respx.post(SINGLE_BUY).mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "pid-77"})
    )
    respx.post(CONFIRMATIONS).mock(return_value=_confirmation(code))
    return respx.post(SHOPIFY).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"orderMarkAsPaid": {"order": {"id": "x", "displayFinancialStatus": "PAID"},
                                               "userErrors": []}}},
        )
    )


async def _link_path(flow_ctx) -> str:
    app, _ = flow_ctx
    await _place_order(app)
    from bims_shopify.adapters.persistence.payment_intent_repository import (
        SqlAlchemyPaymentIntentRepository,
    )

    async with app.state.session_factory() as session:
        rows = await SqlAlchemyPaymentIntentRepository(session).list_recent(1)
    assert len(rows) == 1 and rows[0]["order_id"] == ORDER_ID
    return re.sub(r"^https://pay\.example\.com", "", rows[0]["checkout_url"])


@respx.mock
async def test_full_flow_pays_the_order_once_using_a_shopify_gid(flow):
    app, _ = flow
    shopify = _mock_happy_path()

    path = await _link_path(flow)
    page = await _request(app, "GET", path)
    first = await _request(app, "POST", "/payments/bancard_vpos/acme/callback", json=_callback())
    replay = await _request(app, "POST", "/payments/bancard_vpos/acme/callback", json=_callback())

    assert page.status_code == 200 and "pid-77" in page.text
    assert first.json() == replay.json() == {"status": "success"}
    assert shopify.call_count == 1
    variables = json.loads(shopify.calls[0].request.content)["variables"]
    assert variables == {"input": {"id": f"gid://shopify/Order/{ORDER_ID}"}}


@respx.mock
async def test_forged_callback_never_marks_the_order_paid(flow):
    app, _ = flow
    shopify = _mock_happy_path()
    await _link_path(flow)

    resp = await _request(app, "POST", "/payments/bancard_vpos/acme/callback",
                          json=_callback(token="0" * 32))

    assert (resp.status_code, resp.json()) == (200, {"status": "success"})
    assert shopify.call_count == 0


@respx.mock
async def test_declined_confirmation_never_marks_the_order_paid(flow):
    app, _ = flow
    shopify = _mock_happy_path("05")
    await _link_path(flow)

    resp = await _request(app, "POST", "/payments/bancard_vpos/acme/callback", json=_callback())

    assert resp.status_code == 200
    assert shopify.call_count == 0
