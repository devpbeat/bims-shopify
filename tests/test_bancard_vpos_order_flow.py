"""End to end vPOS flow: orders/create -> link -> iframe page -> confirm callback -> order paid."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import timedelta

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from bims_shopify.api import webhooks
from bims_shopify.checkout_link import derive_link_secret, sign_navigation_token
from bims_shopify.domain.bancard_vpos import VposAmount, confirm_token
from tests.conftest import VPOS_PRIVATE_KEY

STAGING = "https://vpos.infonet.com.py:8888"
SINGLE_BUY = f"{STAGING}/vpos/api/0.3/single_buy"
CONFIRMATIONS = f"{STAGING}/vpos/api/0.3/single_buy/confirmations"
SHOPIFY = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
ORDER_ID = "18943384158340"  # a REAL 14-digit Shopify order id


def _callback(spid: int, *, token: str | None = None) -> dict:
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
    return respx.post(SHOPIFY).mock(side_effect=_shopify)


def _shopify(request: httpx.Request) -> httpx.Response:
    """Answer the live-order read and the mark-as-paid mutation like Shopify would."""
    if b"metafieldsSet" in request.content:
        return httpx.Response(
            200, json={"data": {"metafieldsSet": {"metafields": [], "userErrors": []}}}
        )
    if b"orderMarkAsPaid" in request.content:
        return httpx.Response(
            200,
            json={"data": {"orderMarkAsPaid": {"order": {"id": "x", "displayFinancialStatus": "PAID"},
                                               "userErrors": []}}},
        )
    order = {"id": f"gid://shopify/Order/{ORDER_ID}", "displayFinancialStatus": "PENDING",
             "totalPriceSet": {"shopMoney": {"amount": "130.00", "currencyCode": "PYG"}}}
    return httpx.Response(200, json={"data": {"order": order}})


async def _stored_spid(app) -> int:
    """The shop_process_id minted for the order, read back from the stored intent."""
    from bims_shopify.adapters.persistence.payment_intent_repository import (
        SqlAlchemyPaymentIntentRepository,
    )

    async with app.state.session_factory() as session:
        rows = await SqlAlchemyPaymentIntentRepository(session).list_recent(1)
    spid = int(rows[0]["provider_reference"])
    assert len(str(spid)) <= 15
    return spid


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
    assert not [c for c in respx.calls if str(c.request.url) == SINGLE_BUY]
    page = await _request(app, "GET", path)
    assert len([c for c in respx.calls if str(c.request.url) == SINGLE_BUY]) == 1
    spid = await _stored_spid(app)
    first = await _request(app, "POST", "/payments/bancard_vpos/acme/callback", json=_callback(spid))
    replay = await _request(app, "POST", "/payments/bancard_vpos/acme/callback", json=_callback(spid))

    assert page.status_code == 200 and "pid-77" in page.text
    assert first.json() == replay.json() == {"status": "success"}
    paid = [c for c in shopify.calls if b"orderMarkAsPaid" in c.request.content]
    assert len(paid) == 1
    variables = json.loads(paid[0].request.content)["variables"]
    assert variables == {"input": {"id": f"gid://shopify/Order/{ORDER_ID}"}}


@respx.mock
async def test_forged_callback_never_marks_the_order_paid(flow):
    app, _ = flow
    shopify = _mock_happy_path()
    path = await _link_path(flow)
    await _request(app, "GET", path)

    resp = await _request(app, "POST", "/payments/bancard_vpos/acme/callback",
                          json=_callback(await _stored_spid(app), token="0" * 32))

    assert (resp.status_code, resp.json()) == (200, {"status": "success"})
    assert not [c for c in shopify.calls if b"orderMarkAsPaid" in c.request.content]


@respx.mock
async def test_declined_confirmation_never_marks_the_order_paid(flow):
    app, _ = flow
    shopify = _mock_happy_path("05")
    path = await _link_path(flow)
    await _request(app, "GET", path)

    resp = await _request(app, "POST", "/payments/bancard_vpos/acme/callback",
                          json=_callback(await _stored_spid(app)))

    assert resp.status_code == 200
    assert not [c for c in shopify.calls if b"orderMarkAsPaid" in c.request.content]


@respx.mock
async def test_minted_id_is_row_based_and_fits_vpos_fifteen_digits(flow):
    app, _ = flow
    _mock_happy_path()
    path = await _link_path(flow)

    await _request(app, "GET", path)

    sent = next(c for c in respx.calls if str(c.request.url) == SINGLE_BUY).request
    spid = json.loads(sent.content)["operation"]["shop_process_id"]
    assert spid == await _stored_spid(app)
    assert spid % 100 == 0 and len(str(spid)) <= 15
    assert str(spid) != ORDER_ID + "00"


@respx.mock
async def test_first_page_open_without_a_webhook_creates_the_row_before_minting(flow):
    """orders/create never arrived: the page still opens and mints against a fresh row."""
    app, _ = flow
    _mock_happy_path()
    token = sign_navigation_token(
        derive_link_secret(flow[1]), "acme", ORDER_ID, ttl=timedelta(hours=1)
    )

    page = await _request(app, "GET", f"/pay/bancard_vpos/acme/{token}")

    assert page.status_code == 200 and "pid-77" in page.text
    spid = await _stored_spid(app)
    first = await _request(app, "POST", "/payments/bancard_vpos/acme/callback", json=_callback(spid))
    assert first.json() == {"status": "success"}
    paid = [c for c in respx.calls if b"orderMarkAsPaid" in c.request.content]
    assert len(paid) == 1


@respx.mock
async def test_return_page_resolves_the_order_from_a_row_based_id(flow):
    app, _ = flow
    _mock_happy_path()
    path = await _link_path(flow)
    await _request(app, "GET", path)
    token = path.rsplit("/", 1)[-1]

    resp = await _request(app, "GET", f"/pay/bancard_vpos/acme/return/{token}")

    confirmations = [c for c in respx.calls if str(c.request.url) == CONFIRMATIONS]
    assert len(confirmations) == 1
    sent = json.loads(confirmations[0].request.content)["operation"]["shop_process_id"]
    assert sent == await _stored_spid(app)
    assert resp.status_code in (200, 303)


def _confirmation_with_card() -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "success", "confirmation": {
            "response_code": "00", "amount": "130.00", "currency": "PYG",
            "authorization_number": "123456", "ticket_number": "987654321012345",
            "security_information": {"card_source": "I", "risk_index": "0"},
            "extra_attributes": {"payment_card_brand": "visa", "payment_card_type": "credit",
                                 "payment_masked_card_number": "411111******1111"},
        }},
    )


@respx.mock
async def test_confirmed_callback_writes_the_bancard_reference_after_marking_paid(flow):
    app, _ = flow
    shopify = _mock_happy_path()
    respx.post(CONFIRMATIONS).mock(return_value=_confirmation_with_card())
    await _request(app, "GET", await _link_path(flow))
    spid = await _stored_spid(app)

    for _ in range(2):  # the redelivery must not write a second time
        resp = await _request(app, "POST", "/payments/bancard_vpos/acme/callback", json=_callback(spid))
        assert resp.json() == {"status": "success"}

    ops = [c for c in shopify.calls if b"orderMarkAsPaid" in c.request.content
           or b"metafieldsSet" in c.request.content]
    assert [b"orderMarkAsPaid" in c.request.content for c in ops] == [True, False]
    written = json.loads(ops[1].request.content)["variables"]["metafields"]
    values = {m["key"]: (m["type"], m["value"]) for m in written}
    assert {m["ownerId"] for m in written} == {f"gid://shopify/Order/{ORDER_ID}"}
    assert {m["namespace"] for m in written} == {"bims_bancard"}
    assert values["shop_process_id"] == ("number_integer", str(spid))
    assert values["authorization_number"] == ("single_line_text_field", "123456")
    assert values["ticket_number"] == ("number_integer", "987654321012345")
    assert values["payment_card_brand"] == ("single_line_text_field", "visa")
    assert values["card_source"] == ("single_line_text_field", "I")
    assert values["confirmed_at"][0] == "date_time"


@respx.mock
async def test_metafield_failure_leaves_the_order_paid_with_a_warning_and_audit_entry(flow):
    import sqlalchemy as sa
    from structlog.testing import capture_logs

    from bims_shopify.adapters.persistence.models import AuditLogModel

    app, _ = flow

    def failing(request: httpx.Request) -> httpx.Response:
        if b"metafieldsSet" in request.content:
            return httpx.Response(200, json={"data": {"metafieldsSet": {"metafields": None, "userErrors": [
                {"field": ["metafields"], "message": "denied", "code": "ACCESS_DENIED"}]}}})
        return _shopify(request)

    respx.post(SINGLE_BUY).mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "pid-77"})
    )
    respx.post(CONFIRMATIONS).mock(return_value=_confirmation_with_card())
    shopify = respx.post(SHOPIFY).mock(side_effect=failing)
    await _request(app, "GET", await _link_path(flow))
    spid = await _stored_spid(app)

    with capture_logs() as logs:
        resp = await _request(app, "POST", "/payments/bancard_vpos/acme/callback", json=_callback(spid))

    assert resp.json() == {"status": "success"}
    assert len([c for c in shopify.calls if b"orderMarkAsPaid" in c.request.content]) == 1
    assert "payment_metafields_failed" in [e["event"] for e in logs if e["log_level"] == "warning"]
    async with app.state.session_factory() as session:
        actions = (await session.execute(sa.select(AuditLogModel.action))).scalars().all()
    assert "payment.metafields_failed" in actions
