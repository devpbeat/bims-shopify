"""Explicit, classified vPOS rollback: the operator-facing sibling of abandon_attempt."""
from __future__ import annotations

import json

import httpx
import pytest
import respx
import structlog

from bims_shopify.adapters.payments import bancard_vpos_provider as vpos_module
from bims_shopify.adapters.payments.bancard_vpos_provider import BancardVposProvider
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import (
    RollbackVerdict,
    VposConfigurationError,
    classify_rollback_verdict,
)
from bims_shopify.domain.payment import PaymentIntent
from bims_shopify.domain.tenant import PaymentProvider

PRIVATE_KEY = "PRIVATE-KEY-SECRET"
ROLLBACK = "https://vpos.infonet.com.py:8888/vpos/api/0.3/single_buy/rollback"


@pytest.mark.parametrize(
    ("key", "verdict"),
    [
        ("RollbackSuccessful", RollbackVerdict.SUCCEEDED),
        ("PaymentNotFoundError", RollbackVerdict.NO_OP),
        ("AlreadyRollbackedError", RollbackVerdict.NO_OP),
        ("TransactionAlreadyConfirmed", RollbackVerdict.MANUAL_ANNULMENT_REQUIRED),
        ("PosCommunicationError", RollbackVerdict.UNEXPECTED),
        ("SomethingNew", RollbackVerdict.UNEXPECTED),
        (None, RollbackVerdict.UNEXPECTED),
    ],
)
def test_classify_rollback_verdict(key, verdict):
    assert classify_rollback_verdict(key) is verdict


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.entries.append(kwargs)


@pytest.fixture
def vpos_tenant(tenant):
    tenant.payment_provider = PaymentProvider.BANCARD_VPOS
    tenant.provider_config = {"public_key": "PUB", "environment": "staging"}
    tenant.bancard_vpos_private_key = PRIVATE_KEY
    return tenant


@pytest.fixture
def provider():
    settings = Settings(checkout_link_secret="s", public_base_url="https://a.example", fernet_key="")
    return BancardVposProvider(settings, audit=FakeAudit())


def _intent(spid: str = "100000000000100") -> PaymentIntent:
    return PaymentIntent(
        tenant_slug="acme", order_id="1000000000001", amount=130.0, currency="PYG",
        provider_reference=spid,
    )


def _reply(key: str, status: str = "error") -> httpx.Response:
    return httpx.Response(
        200, json={"status": status, "messages": [{"key": key, "level": "info", "dsc": "d"}]}
    )


@respx.mock
@pytest.mark.parametrize(
    ("key", "verdict"),
    [
        ("RollbackSuccessful", RollbackVerdict.SUCCEEDED),
        ("PaymentNotFoundError", RollbackVerdict.NO_OP),
        ("AlreadyRollbackedError", RollbackVerdict.NO_OP),
        ("TransactionAlreadyConfirmed", RollbackVerdict.MANUAL_ANNULMENT_REQUIRED),
        ("PosCommunicationError", RollbackVerdict.UNEXPECTED),
    ],
)
async def test_rollback_operation_returns_the_classified_outcome(provider, vpos_tenant, key, verdict):
    route = respx.post(ROLLBACK).mock(return_value=_reply(key))

    result = await provider.rollback_operation(vpos_tenant, _intent("12313"))

    assert result.verdict is verdict and result.key == key
    assert json.loads(route.calls.last.request.content)["operation"]["shop_process_id"] == "12313"


@respx.mock
@pytest.mark.parametrize(
    "reply",
    [httpx.Response(500, text="boom"), httpx.Response(200, text="not json"), httpx.ConnectTimeout("slow")],
)
async def test_rollback_operation_reports_transport_failure_without_raising(provider, vpos_tenant, reply):
    respx.post(ROLLBACK).mock(side_effect=[reply])

    result = await provider.rollback_operation(vpos_tenant, _intent())

    assert result.verdict is RollbackVerdict.TRANSPORT_FAILURE and result.key is None


@respx.mock
async def test_invalid_token_is_a_configuration_error(provider, vpos_tenant):
    respx.post(ROLLBACK).mock(return_value=_reply("InvalidTokenError"))

    with pytest.raises(VposConfigurationError):
        await provider.rollback_operation(vpos_tenant, _intent())


@respx.mock
async def test_missing_credentials_raise_configuration_error_without_http(provider, vpos_tenant):
    vpos_tenant.bancard_vpos_private_key = None

    with pytest.raises(VposConfigurationError):
        await provider.rollback_operation(vpos_tenant, _intent())
    assert not respx.calls


@respx.mock
async def test_rollback_operation_never_logs_key_material(provider, vpos_tenant, monkeypatch):
    monkeypatch.setattr(vpos_module, "logger", structlog.get_logger("vpos-test"))
    respx.post(ROLLBACK).mock(return_value=_reply("TransactionAlreadyConfirmed"))

    with structlog.testing.capture_logs() as logs:
        await provider.rollback_operation(vpos_tenant, _intent())

    assert PRIVATE_KEY not in json.dumps(logs, default=str)
