"""Tests for PagoparProvider against the real pagopar-sdk HTTP surface (mocked via respx)."""
from __future__ import annotations

import httpx
import pytest
import respx

from bims_shopify.adapters.payments.pagopar_provider import (
    PagoparCallbackTokenError,
    PagoparProvider,
)
from bims_shopify.domain.payment import PaymentIntent, PaymentStatus


@pytest.fixture
def pagopar_tenant(tenant):
    tenant.provider_config = {
        "public_key": "pub-123",
        "private_key": "priv-456",
    }
    return tenant


@respx.mock
async def test_create_checkout_returns_hosted_checkout_url(pagopar_tenant):
    respx.post("https://api.pagopar.com/api/comercios/2.0/iniciar-transaccion").mock(
        return_value=httpx.Response(
            200,
            json={
                "respuesta": True,
                "resultado": [{"data": "abc123", "pedido": "1750"}],
            },
        )
    )
    provider = PagoparProvider()

    intent = await provider.create_checkout(pagopar_tenant, "1001", 100.0, "PYG")

    assert intent.provider_reference == "abc123"
    assert intent.checkout_url == "https://www.pagopar.com/pagos/abc123"
    assert intent.order_id == "1001"


@respx.mock
async def test_verify_payment_confirmed_when_pagado_true(pagopar_tenant):
    respx.post("https://api.pagopar.com/api/pedidos/1.1/traer").mock(
        return_value=httpx.Response(
            200,
            json={"respuesta": True, "resultado": [{"pagado": True, "cancelado": False}]},
        )
    )
    provider = PagoparProvider()
    intent = PaymentIntent(
        tenant_slug="acme", order_id="1001", amount=100.0, currency="PYG",
        provider_reference="abc123",
    )

    status = await provider.verify_payment(pagopar_tenant, intent)

    assert status == PaymentStatus.CONFIRMED


@respx.mock
async def test_verify_payment_pending_when_not_pagado_and_not_cancelado(pagopar_tenant):
    respx.post("https://api.pagopar.com/api/pedidos/1.1/traer").mock(
        return_value=httpx.Response(
            200,
            json={"respuesta": True, "resultado": [{"pagado": False, "cancelado": False}]},
        )
    )
    provider = PagoparProvider()
    intent = PaymentIntent(
        tenant_slug="acme", order_id="1001", amount=100.0, currency="PYG",
        provider_reference="abc123",
    )

    status = await provider.verify_payment(pagopar_tenant, intent)

    assert status == PaymentStatus.PENDING

@respx.mock
async def test_verify_payment_failed_when_cancelado_true(pagopar_tenant):
    respx.post("https://api.pagopar.com/api/pedidos/1.1/traer").mock(
        return_value=httpx.Response(
            200,
            json={"respuesta": True, "resultado": [{"pagado": False, "cancelado": True}]},
        )
    )
    provider = PagoparProvider()
    intent = PaymentIntent(
        tenant_slug="acme", order_id="1001", amount=100.0, currency="PYG",
        provider_reference="abc123",
    )

    status = await provider.verify_payment(pagopar_tenant, intent)

    assert status == PaymentStatus.FAILED


async def test_parse_callback_extracts_hash_pedido_without_trusting_status(pagopar_tenant):
    provider = PagoparProvider()
    payload = {
        "respuesta": True,
        "resultado": [
            {
                "hash_pedido": "abc123",
                "numero_pedido": "1750",
                "monto": "100.00",
                "pagado": True,
                "cancelado": False,
            }
        ],
    }

    intent = await provider.parse_callback(pagopar_tenant, payload)

    assert intent.provider_reference == "abc123"
    # Pagopar's callback never echoes the merchant's own order id.
    assert intent.order_id == ""
    # Status is never taken from the callback body itself.
    assert intent.status == "pending"

async def test_parse_callback_rejects_bad_token(pagopar_tenant):
    provider = PagoparProvider()
    payload = {
        "resultado": [{"hash_pedido": "abc123", "token": "not-the-real-sha1", "pagado": True}]
    }

    with pytest.raises(PagoparCallbackTokenError):
        await provider.parse_callback(pagopar_tenant, payload)

async def test_parse_callback_accepts_valid_token(pagopar_tenant):
    import pagopar_sdk

    provider = PagoparProvider()
    valid_token = pagopar_sdk.build_token("priv-456", "abc123")
    payload = {"resultado": [{"hash_pedido": "abc123", "token": valid_token, "pagado": True}]}

    intent = await provider.parse_callback(pagopar_tenant, payload)

    assert intent.provider_reference == "abc123"
