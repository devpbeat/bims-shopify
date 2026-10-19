"""The vPOS charge amount comes from the Shopify order, never from the request body."""
from __future__ import annotations

import json

import httpx
import respx
from httpx import ASGITransport, AsyncClient

from bims_shopify.domain.tenant import PaymentProvider

SHOPIFY = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
SINGLE_BUY = "https://vpos.infonet.com.py:8888/vpos/api/0.3/single_buy"
GID = "gid://shopify/Order/1001"


def _order(amount: str = "150000.00", currency: str = "PYG") -> httpx.Response:
    order = {"id": GID, "totalPriceSet": {"shopMoney": {"amount": amount, "currencyCode": currency}}}
    return httpx.Response(200, json={"data": {"order": order}})


async def _checkout(app, provider: str, body: dict) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/payments/{provider}/acme/checkout", json=body)


def _mock_single_buy():
    return respx.post(SINGLE_BUY).mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "pid-1"})
    )


@respx.mock
async def test_vpos_charges_the_shopify_total_and_ignores_the_body_amount(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant()
    shopify = respx.post(SHOPIFY).mock(return_value=_order("150000.50"))
    single_buy = _mock_single_buy()

    resp = await _checkout(app, "bancard_vpos", {"order_id": "1001", "amount": 1, "currency": "USD"})

    assert resp.status_code == 200
    assert json.loads(shopify.calls[0].request.content)["variables"] == {"id": GID}
    assert json.loads(single_buy.calls[0].request.content)["operation"]["amount"] == "150000.50"


@respx.mock
async def test_non_pyg_order_is_rejected_before_any_bancard_call(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant()
    respx.post(SHOPIFY).mock(return_value=_order("10.00", "USD"))
    single_buy = _mock_single_buy()

    resp = await _checkout(app, "bancard_vpos", {"order_id": "1001"})

    assert resp.status_code == 422
    assert single_buy.call_count == 0


@respx.mock
async def test_missing_order_is_not_found(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant()
    respx.post(SHOPIFY).mock(return_value=httpx.Response(200, json={"data": {"order": None}}))
    single_buy = _mock_single_buy()

    resp = await _checkout(app, "bancard_vpos", {"order_id": "1001"})

    assert resp.status_code == 404
    assert single_buy.call_count == 0


@respx.mock
async def test_shopify_failure_is_a_generic_bad_gateway(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant()
    respx.post(SHOPIFY).mock(return_value=httpx.Response(400, json={}))
    single_buy = _mock_single_buy()

    resp = await _checkout(app, "bancard_vpos", {"order_id": "1001"})

    assert resp.status_code == 502
    assert "acme" not in resp.text
    assert single_buy.call_count == 0


@respx.mock
async def test_pagopar_keeps_its_body_amount_and_never_asks_shopify(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant(
        payment_provider=PaymentProvider.PAGOPAR,
        provider_config={"public_key": "pub", "private_key": "priv"},
    )
    shopify = respx.post(SHOPIFY).mock(return_value=_order())
    respx.post("https://api.pagopar.com/api/comercios/2.0/iniciar-transaccion").mock(
        return_value=httpx.Response(
            200, json={"respuesta": True, "resultado": [{"data": "hash-1", "pedido": "1"}]}
        )
    )

    resp = await _checkout(app, "pagopar", {"order_id": "1001", "amount": 5000, "currency": "PYG"})

    assert resp.status_code == 200
    assert shopify.call_count == 0
