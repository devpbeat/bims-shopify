"""Tests for BancardVposProvider against a mocked vPOS server via respx."""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal

import httpx
import pytest
import respx
import structlog

from bims_shopify.adapters.payments.bancard_vpos_provider import (
    BancardVposProvider,
    VposCallbackTokenError,
)
from bims_shopify.checkout_link import verify_token
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import (
    VposApiError,
    VposCallbackShapeError,
    VposConfigurationError,
    VposInvalidIdError,
)
from bims_shopify.domain.payment import PaymentIntent, PaymentStatus
from bims_shopify.domain.tenant import PaymentProvider

PRIVATE_KEY = "PRIVATE-KEY-SECRET"
STAGING = "https://vpos.infonet.com.py:8888"
PRODUCTION = "https://vpos.infonet.com.py"
SECRET = "link-secret"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


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
def audit():
    return FakeAudit()


def _intent(spid: str = "100000000000100", amount: float = 130.0) -> PaymentIntent:
    return PaymentIntent(
        tenant_slug="acme",
        order_id=str(int(spid) // 100),
        amount=amount,
        currency="PYG",
        provider_reference=spid,
    )


@pytest.fixture
def provider(audit):
    settings = Settings(
        checkout_link_secret=SECRET, public_base_url="https://app.example.com", fernet_key=""
    )
    return BancardVposProvider(settings, audit=audit)




# --- create_checkout ---------------------------------------------------------


@respx.mock
async def test_create_checkout_posts_single_buy_envelope(provider, vpos_tenant):
    route = respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "PROC1"})
    )

    # A REAL 14-digit Shopify order id (the production outage): the id comes from the row.
    intent = await provider.create_checkout(
        vpos_tenant, "18943384158340", 130, "PYG", row_id=4242
    )

    body = json.loads(route.calls.last.request.content)
    assert set(body) == {"public_key", "operation"}
    op = body["operation"]
    assert op["shop_process_id"] == 424200
    assert isinstance(op["shop_process_id"], int)
    assert len(str(op["shop_process_id"])) <= 15
    assert op["amount"] == "130.00"
    assert op["currency"] == "PYG"
    assert op["token"] == _md5(PRIVATE_KEY + "424200" + "130.00" + "PYG")
    assert intent.provider_reference == "424200"
    assert intent.status == "pending"
    assert intent.order_id == "18943384158340"


@respx.mock
async def test_create_checkout_accepts_fifteen_digit_order_ids(provider, vpos_tenant):
    route = respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "PROC1"})
    )

    intent = await provider.create_checkout(
        vpos_tenant, "189433841583401", 130, "PYG", row_id=7
    )

    assert json.loads(route.calls.last.request.content)["operation"]["shop_process_id"] == 700
    assert intent.order_id == "189433841583401"


@respx.mock
async def test_create_checkout_truncates_description_and_builds_urls(provider, vpos_tenant):
    route = respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "P"})
    )

    await provider.create_checkout(vpos_tenant, "1001", 100.0, "PYG", row_id=1001)

    op = json.loads(route.calls.last.request.content)["operation"]
    assert len(op["description"]) <= 20
    assert op["return_url"].startswith("https://app.example.com/")
    assert op["cancel_url"].startswith("https://app.example.com/")
    assert op["return_url"] != op["cancel_url"]
    nav = op["return_url"].rsplit("/", 1)[-1]
    claims = verify_token(SECRET, nav, "acme")
    assert claims.order_id == "1001" and claims.process_id is None


@respx.mock
async def test_create_checkout_url_is_signed_own_page(provider, vpos_tenant):
    respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "PROC1"})
    )

    intent = await provider.create_checkout(vpos_tenant, "1001", 100.0, "PYG", row_id=1001)

    prefix = "https://app.example.com/pay/bancard_vpos/acme/"
    assert intent.checkout_url.startswith(prefix)
    assert "vpos.infonet.com.py" not in intent.checkout_url
    claims = verify_token(SECRET, intent.checkout_url[len(prefix):], "acme")
    assert claims.process_id is None
    assert claims.order_id == "1001"
    assert intent.process_id == "PROC1"


@respx.mock
async def test_create_checkout_attempt_changes_id(provider, vpos_tenant):
    route = respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "P"})
    )

    intent = await provider.create_checkout(vpos_tenant, "1001", 100.0, "PYG", row_id=1001, attempt=1)

    assert intent.provider_reference == "100101"
    assert json.loads(route.calls.last.request.content)["operation"]["shop_process_id"] == 100101


@respx.mock
async def test_create_checkout_uses_each_tenants_environment(provider, vpos_tenant):
    staging = respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "S"})
    )
    production = respx.post(f"{PRODUCTION}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "P"})
    )
    prod_tenant = copy.deepcopy(vpos_tenant)
    prod_tenant.provider_config = {"public_key": "PUB", "environment": "production"}

    await provider.create_checkout(vpos_tenant, "1001", 100.0, "PYG", row_id=1001)
    await provider.create_checkout(prod_tenant, "1002", 100.0, "PYG", row_id=1002)

    assert staging.call_count == 1 and production.call_count == 1


@respx.mock
async def test_create_checkout_error_does_not_leak_private_key(provider, vpos_tenant):
    respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "error",
                "messages": [{"key": "SomethingBroke", "level": "error", "dsc": "bad"}],
            },
        )
    )

    with pytest.raises(VposApiError) as excinfo:
        await provider.create_checkout(vpos_tenant, "1001", 100.0, "PYG", row_id=1001)

    assert PRIVATE_KEY not in str(excinfo.value)


@respx.mock
async def test_create_checkout_missing_process_id_raises(provider, vpos_tenant):
    respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )

    with pytest.raises(VposApiError):
        await provider.create_checkout(vpos_tenant, "1001", 100.0, "PYG", row_id=1001)


@respx.mock
async def test_create_checkout_invalid_token_is_configuration_error(provider, vpos_tenant):
    respx.post(f"{STAGING}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(
            200,
            json={"status": "error", "messages": [{"key": "InvalidTokenError", "level": "error", "dsc": "x"}]},
        )
    )

    with pytest.raises(VposConfigurationError):
        await provider.create_checkout(vpos_tenant, "1001", 100.0, "PYG", row_id=1001)


@respx.mock
async def test_create_checkout_rejects_bad_ids_before_http(provider, vpos_tenant):
    route = respx.post(f"{STAGING}/vpos/api/0.3/single_buy")

    with pytest.raises(VposInvalidIdError):
        await provider.create_checkout(vpos_tenant, "gid://shopify/Order/1", 100.0, "PYG", row_id=0)

    assert route.call_count == 0


@respx.mock
async def test_create_checkout_requires_private_key_before_http(provider, vpos_tenant):
    vpos_tenant.bancard_vpos_private_key = None
    route = respx.post(f"{STAGING}/vpos/api/0.3/single_buy")

    with pytest.raises(VposConfigurationError):
        await provider.create_checkout(vpos_tenant, "1001", 100.0, "PYG", row_id=1001)

    assert route.call_count == 0


# --- parse_callback ----------------------------------------------------------


def _callback(token: str | None = None, *, numbers: bool = False) -> dict:
    token = token or _md5(PRIVATE_KEY + "12313" + "confirm" + "10100.00" + "PYG")
    if numbers:
        fields = {
            "shop_process_id": 12313,
            "amount": 10100.00,
            "iva_amount": 1100.0,
            "ticket_number": 123456789123456,
        }
    else:
        fields = {
            "shop_process_id": "12313",
            "amount": "10100.00",
            "iva_amount": "1100.0",
            "ticket_number": "123456789123456",
        }
    return {"operation": {"token": token, "currency": "PYG", **fields}}


@pytest.mark.parametrize("numbers", [False, True])
async def test_parse_callback_valid(provider, vpos_tenant, numbers):
    intent = await provider.parse_callback(vpos_tenant, _callback(numbers=numbers))

    # The callback never names the order: the use case resolves it by provider_reference.
    assert intent.order_id == ""
    assert intent.provider_reference == "12313"
    assert Decimal(str(intent.amount)) == Decimal("10100.00")
    assert intent.currency == "PYG"
    assert intent.status == "pending"


async def test_parse_callback_tampered_token(provider, vpos_tenant):
    with pytest.raises(VposCallbackTokenError):
        await provider.parse_callback(vpos_tenant, _callback("0" * 32))


async def test_parse_callback_equal_length_mismatch_uses_compare_digest(
    provider, vpos_tenant, monkeypatch
):
    calls = []
    import hmac as hmac_module

    real = hmac_module.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac_module, "compare_digest", spy)

    with pytest.raises(VposCallbackTokenError):
        await provider.parse_callback(vpos_tenant, _callback("f" * 32))

    assert calls


@pytest.mark.parametrize("payload", [{}, {"operation": "x"}, {"operation": {"token": "t"}}])
async def test_parse_callback_malformed(provider, vpos_tenant, payload):
    with pytest.raises(VposCallbackShapeError):
        await provider.parse_callback(vpos_tenant, payload)


async def test_parse_callback_requires_private_key(provider, vpos_tenant):
    vpos_tenant.bancard_vpos_private_key = None

    with pytest.raises(VposConfigurationError):
        await provider.parse_callback(vpos_tenant, _callback())


# --- verify_payment ----------------------------------------------------------

CONFIRMATIONS = f"{STAGING}/vpos/api/0.3/single_buy/confirmations"


def _confirmation(code="00", amount="130.00", **extra) -> dict:
    confirmation = {
        "token": "t" * 32,
        "shop_process_id": 100000000000100,
        "response": "S",
        "response_code": code,
        "amount": amount,
        "currency": "PYG",
        "security_information": {"card_source": "I", "risk_index": "0"},
    }
    if code == "00":
        confirmation["authorization_number"] = "123456"
    confirmation.update(extra)
    return {"status": "success", "confirmation": confirmation}


@respx.mock
async def test_verify_payment_request_shape(provider, vpos_tenant):
    route = respx.post(CONFIRMATIONS).mock(return_value=httpx.Response(200, json=_confirmation()))

    status = await provider.verify_payment(vpos_tenant, _intent())

    assert status == PaymentStatus.CONFIRMED
    assert route.calls.last.request.url.path == "/vpos/api/0.3/single_buy/confirmations"
    body = json.loads(route.calls.last.request.content)
    assert set(body) == {"public_key", "operation"}
    assert body["operation"]["shop_process_id"] == 100000000000100
    assert isinstance(body["operation"]["shop_process_id"], int)
    assert body["operation"]["token"] == _md5(PRIVATE_KEY + "100000000000100" + "get_confirmation")


@pytest.mark.parametrize("code", ["05", "12", "15", "51"])
@respx.mock
async def test_verify_payment_declined_codes_fail(provider, vpos_tenant, code):
    respx.post(CONFIRMATIONS).mock(return_value=httpx.Response(200, json=_confirmation(code)))

    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.FAILED


@pytest.mark.parametrize("code", ["99", None])
@respx.mock
async def test_verify_payment_unknown_code_is_pending(provider, vpos_tenant, code):
    payload = _confirmation(code or "99")
    if code is None:
        del payload["confirmation"]["response_code"]
    respx.post(CONFIRMATIONS).mock(return_value=httpx.Response(200, json=payload))

    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.PENDING


@respx.mock
async def test_verify_payment_response_flag_never_decides(provider, vpos_tenant):
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(200, json=_confirmation("05", response="S"))
    )
    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.FAILED

    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(200, json=_confirmation("00", response="N"))
    )
    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.CONFIRMED


@respx.mock
async def test_verify_payment_declined_without_authorization_parses(provider, vpos_tenant):
    payload = _confirmation("05")
    assert "authorization_number" not in payload["confirmation"]
    respx.post(CONFIRMATIONS).mock(return_value=httpx.Response(200, json=payload))

    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.FAILED


@respx.mock
async def test_verify_payment_tolerates_absent_messages_and_numeric_amount(provider, vpos_tenant):
    payload = _confirmation(amount=130)
    assert "messages" not in payload
    respx.post(CONFIRMATIONS).mock(return_value=httpx.Response(200, json=payload))

    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.CONFIRMED


@respx.mock
async def test_verify_payment_amount_mismatch_fails_and_audits(provider, vpos_tenant, audit):
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(200, json=_confirmation(amount="130.00"))
    )

    with structlog.testing.capture_logs() as logs:
        status = await provider.verify_payment(vpos_tenant, _intent(amount=200.0))

    assert status == PaymentStatus.FAILED
    assert any(entry["log_level"] == "error" for entry in logs)
    assert [e["action"] for e in audit.entries] == ["payment.amount_mismatch"]
    assert PRIVATE_KEY not in json.dumps(audit.entries, default=str)


@respx.mock
async def test_verify_payment_currency_mismatch_fails(provider, vpos_tenant):
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(200, json=_confirmation(currency="USD"))
    )

    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.FAILED


@pytest.mark.parametrize("key", ["BuyNotFoundError", "PaymentNotFoundError"])
@respx.mock
async def test_verify_payment_not_found_is_pending_not_declined(provider, vpos_tenant, key):
    # Bancard: PaymentNotFoundError means the buyer has not paid (yet) and "must be taken
    # as a correct answer". It is never a decline, which only arrives as a declined code.
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": key, "level": "error", "dsc": "x"}]}
        )
    )

    with structlog.testing.capture_logs() as logs:
        status = await provider.verify_payment(vpos_tenant, _intent())

    assert status == PaymentStatus.PENDING
    assert [e["key"] for e in logs if e["event"] == "vpos_payment_not_found"] == [key]


@respx.mock
async def test_declined_code_fails_while_not_found_key_stays_pending(provider, vpos_tenant):
    route = respx.post(CONFIRMATIONS)
    route.mock(return_value=httpx.Response(200, json=_confirmation("05")))
    declined = await provider.verify_payment(vpos_tenant, _intent())
    route.mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": "PaymentNotFoundError", "level": "error"}]}
        )
    )
    unpaid = await provider.verify_payment(vpos_tenant, _intent())

    assert (declined, unpaid) == (PaymentStatus.FAILED, PaymentStatus.PENDING)


@pytest.mark.parametrize(
    "key", ["InvalidTokenError", "InvalidPublicKeyError", "UnauthorizedOperationError"]
)
@respx.mock
async def test_verify_payment_misconfiguration_raises(provider, vpos_tenant, key):
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": key, "level": "error", "dsc": "x"}]}
        )
    )

    with pytest.raises(VposConfigurationError) as excinfo:
        await provider.verify_payment(vpos_tenant, _intent())

    assert PRIVATE_KEY not in str(excinfo.value)


@respx.mock
async def test_verify_payment_unknown_error_key_is_not_paid(provider, vpos_tenant):
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(
            200,
            json={"status": "error", "messages": [{"key": "Brand", "level": "error", "dsc": "x"}]},
        )
    )

    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.PENDING


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="down"),
        httpx.Response(200, text="not json"),
        httpx.ConnectTimeout("slow"),
    ],
)
@respx.mock
async def test_verify_payment_transport_failure_keeps_pending(provider, vpos_tenant, response):
    respx.post(CONFIRMATIONS).mock(side_effect=[response])

    assert await provider.verify_payment(vpos_tenant, _intent()) == PaymentStatus.PENDING


@respx.mock
async def test_verify_payment_logs_never_contain_secrets(provider, vpos_tenant):
    respx.post(CONFIRMATIONS).mock(return_value=httpx.Response(200, json=_confirmation()))

    with structlog.testing.capture_logs() as logs:
        await provider.verify_payment(vpos_tenant, _intent())

    dumped = json.dumps(logs, default=str)
    assert PRIVATE_KEY not in dumped
    assert "123456" not in dumped


# --- abandon_attempt (rollback) ----------------------------------------------

ROLLBACK = f"{STAGING}/vpos/api/0.3/single_buy/rollback"


def _rollback_reply(key: str, status: str = "error", level: str = "error") -> httpx.Response:
    return httpx.Response(
        200, json={"status": status, "messages": [{"key": key, "level": level, "dsc": "d"}]}
    )


@respx.mock
async def test_rollback_envelope_and_token(provider, vpos_tenant):
    route = respx.post(ROLLBACK).mock(
        return_value=_rollback_reply("RollbackSuccessful", "success", "info")
    )

    await provider.abandon_attempt(vpos_tenant, _intent("12313"))

    body = json.loads(route.calls.last.request.content)
    assert set(body) == {"public_key", "elements", "operation"}
    assert body["elements"] == {}
    assert set(body["operation"]) == {"token", "shop_process_id"}
    assert body["operation"]["shop_process_id"] == "12313"
    assert body["operation"]["token"] == _md5(PRIVATE_KEY + "12313" + "rollback" + "0.00")


@respx.mock
@pytest.mark.parametrize(
    ("key", "status"),
    [
        ("RollbackSuccessful", "success"),
        ("PaymentNotFoundError", "error"),
        ("AlreadyRollbackedError", "error"),
    ],
)
async def test_benign_rollback_outcomes_emit_no_alarm(provider, vpos_tenant, audit, key, status):
    respx.post(ROLLBACK).mock(return_value=_rollback_reply(key, status))

    with structlog.testing.capture_logs() as logs:
        await provider.abandon_attempt(vpos_tenant, _intent())

    assert not [e for e in logs if e["log_level"] in {"warning", "error", "critical"}]
    assert audit.entries == []


@respx.mock
async def test_already_confirmed_rollback_logs_error_and_audits(provider, vpos_tenant, audit):
    respx.post(ROLLBACK).mock(return_value=_rollback_reply("TransactionAlreadyConfirmed"))

    with structlog.testing.capture_logs() as logs:
        await provider.abandon_attempt(vpos_tenant, _intent())

    errors = [e for e in logs if e["log_level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["shop_process_id"] == 100000000000100
    assert errors[0]["tenant"] == "acme"
    assert [e["action"] for e in audit.entries] == ["payment.rollback_conflict"]
    assert audit.entries[0]["tenant_id"] == vpos_tenant.id
    assert audit.entries[0]["payload"]["order_id"] == "1000000000001"
    assert PRIVATE_KEY not in json.dumps([logs, audit.entries], default=str)


@respx.mock
@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        _rollback_reply("PosCommunicationError"),
        _rollback_reply("SomethingNew"),
        httpx.ConnectTimeout("slow"),
    ],
)
async def test_other_rollback_failures_warn_and_never_raise(provider, vpos_tenant, audit, reply):
    respx.post(ROLLBACK).mock(side_effect=[reply])

    with structlog.testing.capture_logs() as logs:
        await provider.abandon_attempt(vpos_tenant, _intent())

    assert [e for e in logs if e["log_level"] == "warning"]
    assert not [e for e in logs if e["log_level"] == "error"]
    assert audit.entries == []


@respx.mock
async def test_rollback_without_credentials_never_raises(provider, vpos_tenant):
    vpos_tenant.bancard_vpos_private_key = None

    with structlog.testing.capture_logs() as logs:
        await provider.abandon_attempt(vpos_tenant, _intent())

    assert [e for e in logs if e["log_level"] == "warning"]
