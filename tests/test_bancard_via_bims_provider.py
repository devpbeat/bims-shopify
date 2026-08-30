"""Tests for BancardViaBIMSProvider against a mocked BIMS server via respx."""
from __future__ import annotations

import httpx
import pytest
import respx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.payments.bancard_via_bims_provider import (
    BancardViaBIMSProvider,
)
from bims_shopify.domain.payment import PaymentIntent, PaymentStatus


@pytest.fixture
def bancard_tenant(tenant):
    tenant.provider_config = {
        "create_link_fields": {
            "monto": "{amount}",
            "moneda": "{currency}",
            "id_pedido": "{order_id}",
            "descripcion": "{description}",
            "url_retorno": "{return_url}",
        },
        "return_url": "https://acme.myshopify.com/thank-you",
        "confirmed_status_values": ["confirmed", "paid"],
    }
    return tenant


@respx.mock
async def test_create_checkout_posts_rendered_fields(bancard_tenant):
    route = respx.post("https://bims.example.com/api/bims_pay/create_link.json").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "data": {"link_url": "https://pay.example/xyz"}}
        )
    )
    client = BIMSClient(bancard_tenant, http_client=httpx.AsyncClient(base_url=bancard_tenant.bims_base_url))
    provider = BancardViaBIMSProvider(client)

    intent = await provider.create_checkout(bancard_tenant, "1001", 100.0, "PYG")

    assert intent.checkout_url == "https://pay.example/xyz"
    assert intent.provider_reference == "1001"
    sent_body = route.calls.last.request.content.decode()
    assert "data%5Bmonto%5D=100.0" in sent_body
    assert "data%5Bid_pedido%5D=1001" in sent_body


@respx.mock
async def test_verify_payment_confirmed_from_lookup(bancard_tenant):
    respx.get("https://bims.example.com/api/bancard_transactions/lookup/1001.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": {"status": "confirmed"}})
    )
    client = BIMSClient(bancard_tenant, http_client=httpx.AsyncClient(base_url=bancard_tenant.bims_base_url))
    provider = BancardViaBIMSProvider(client)
    intent = PaymentIntent(
        tenant_slug="acme", order_id="1001", amount=100.0, currency="PYG", provider_reference="1001"
    )

    status = await provider.verify_payment(bancard_tenant, intent)

    assert status == PaymentStatus.CONFIRMED


@respx.mock
async def test_verify_payment_failed_for_unlisted_status(bancard_tenant):
    respx.get("https://bims.example.com/api/bancard_transactions/lookup/1001.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": {"status": "rejected"}})
    )
    client = BIMSClient(bancard_tenant, http_client=httpx.AsyncClient(base_url=bancard_tenant.bims_base_url))
    provider = BancardViaBIMSProvider(client)
    intent = PaymentIntent(
        tenant_slug="acme", order_id="1001", amount=100.0, currency="PYG", provider_reference="1001"
    )

    status = await provider.verify_payment(bancard_tenant, intent)

    assert status == PaymentStatus.FAILED


async def test_parse_callback_handles_get_style_query_params(bancard_tenant):
    client = BIMSClient(bancard_tenant, http_client=httpx.AsyncClient(base_url=bancard_tenant.bims_base_url))
    provider = BancardViaBIMSProvider(client)

    intent = await provider.parse_callback(bancard_tenant, {"shop_process_id": "1001"})

    assert intent.order_id == "1001"
    assert intent.provider_reference == "1001"
    assert intent.status == "pending"
