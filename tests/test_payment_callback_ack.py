"""vPOS callbacks are always acknowledged; Pagopar and legacy Bancard behave as before."""
from __future__ import annotations

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from bims_shopify.domain.bancard_vpos import VposAmount, confirm_token
from tests.conftest import VPOS_PRIVATE_KEY

STAGING = "https://vpos.infonet.com.py:8888"
CONFIRMATIONS = f"{STAGING}/vpos/api/0.3/single_buy/confirmations"
SHOPIFY = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
ACK = {"status": "success"}


SPID = 424200


async def _seed_intent(app, order_id: str = "18943384158340") -> None:
    """Persist the row a minted operation leaves behind: the callback resolves it by reference."""
    from bims_shopify.adapters.persistence.payment_intent_repository import (
        SqlAlchemyPaymentIntentRepository,
    )
    from bims_shopify.domain.payment import PaymentIntent

    async with app.state.session_factory() as session:
        await SqlAlchemyPaymentIntentRepository(session).save(
            1,
            "bancard_vpos",
            PaymentIntent(
                tenant_slug="acme", order_id=order_id, amount=130.0, currency="PYG",
                provider_reference=str(SPID), process_id="pid-1",
            ),
        )


def _callback(spid: int = SPID, amount: str = "130.00", *, token: str | None = None) -> dict:
    good = confirm_token(VPOS_PRIVATE_KEY, spid, VposAmount.from_order_total(amount))
    return {
        "operation": {
            "token": token or good,
            "shop_process_id": str(spid),
            "response": "S",
            "response_details": "Procesado Correctamente",
            "amount": amount,
            "currency": "PYG",
            "authorization_number": "123456",
            "ticket_number": "123456789123456",
            "response_code": "00",
            "response_description": "Transaccion aprobada",
            "iva_amount": "0.0",
            "extended_response_description": None,
            "security_information": {"customer_ip": "1.1.1.1", "card_source": "L",
                                     "card_country": "PARAGUAY", "version": "0.3",
                                     "risk_index": "0"},
        }
    }


def _confirmation(code: str = "00", amount: str = "130.00") -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "success",
              "confirmation": {"response_code": code, "amount": amount, "currency": "PYG"}},
    )


def _shopify_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": {"orderMarkAsPaid": {"order": {"id": "1", "displayFinancialStatus": "PAID"},
                                           "userErrors": []}}},
    )


def _operation_count(route, operation: str) -> int:
    return sum(1 for call in route.calls if operation in call.request.content.decode())


async def _post(app, provider: str, **kwargs) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/payments/{provider}/acme/callback", **kwargs)


@respx.mock
async def test_verified_callback_is_acked_and_paid_once_even_on_replay(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant()
    await _seed_intent(app)
    respx.post(CONFIRMATIONS).mock(return_value=_confirmation())
    shopify = respx.post(SHOPIFY).mock(return_value=_shopify_ok())

    first = await _post(app, "bancard_vpos", json=_callback())
    replay = await _post(app, "bancard_vpos", json=_callback())

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == ACK
    # Count by operation: the callback also writes reconciliation metafields, so the
    # route's raw call count is not the "paid once" signal. The mutation count is.
    assert _operation_count(shopify, "orderMarkAsPaid") == 1
    assert _operation_count(shopify, "metafieldsSet") == 1


@respx.mock
async def test_non_zero_confirmation_is_acked_but_not_paid(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant()
    respx.post(CONFIRMATIONS).mock(return_value=_confirmation("05"))
    shopify = respx.post(SHOPIFY).mock(return_value=_shopify_ok())

    resp = await _post(app, "bancard_vpos", json=_callback())

    assert (resp.status_code, resp.json()) == (200, ACK)
    assert shopify.call_count == 0


@respx.mock
@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": _callback(token="0" * 32)},
        {"content": b"not json", "headers": {"content-type": "application/json"}},
        {"json": {"operation": dict(_callback()["operation"], shop_process_id="abc")}},
        {"json": ["unexpected"]},
        {"json": {}},
    ],
)
async def test_unverifiable_callbacks_are_acked_and_never_paid(vpos_app, kwargs):
    app, _, make_tenant = vpos_app
    await make_tenant()
    confirmations = respx.post(CONFIRMATIONS).mock(return_value=_confirmation())
    shopify = respx.post(SHOPIFY).mock(return_value=_shopify_ok())

    resp = await _post(app, "bancard_vpos", **kwargs)

    assert (resp.status_code, resp.json()) == (200, ACK)
    assert confirmations.call_count == 0
    assert shopify.call_count == 0


@respx.mock
async def test_unreachable_confirmation_is_acked_and_leaves_order_unpaid(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant()
    respx.post(CONFIRMATIONS).mock(side_effect=httpx.ConnectTimeout("slow"))
    shopify = respx.post(SHOPIFY).mock(return_value=_shopify_ok())

    resp = await _post(app, "bancard_vpos", json=_callback())

    assert (resp.status_code, resp.json()) == (200, ACK)
    assert shopify.call_count == 0


def test_callback_response_hook_defaults_to_intent_status():
    from bims_shopify.domain.payment import PaymentIntent
    from bims_shopify.ports.payment import callback_response_for

    class Plain:
        name = "plain"

    intent = PaymentIntent(tenant_slug="acme", order_id="1", amount=1.0, currency="PYG",
                           status="pending")
    assert callback_response_for(Plain(), intent) == {"status": "pending"}


async def test_pagopar_failures_still_propagate(vpos_app):
    app, _, make_tenant = vpos_app
    from bims_shopify.domain.tenant import PaymentProvider

    await make_tenant(
        payment_provider=PaymentProvider.PAGOPAR,
        provider_config={"public_key": "p", "private_key": "q"},
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/payments/pagopar/acme/callback",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code >= 400
