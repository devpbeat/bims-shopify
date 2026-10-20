"""Writing the confirmed Bancard reference back onto the Shopify order as metafields."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from bims_shopify.adapters.payments import bancard_vpos_provider as provider_module
from bims_shopify.adapters.payments.bancard_vpos_provider import BancardVposProvider
from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.application.payments import HandlePaymentCallback
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import (
    METAFIELD_NAMESPACE,
    confirmation_metafields,
)
from bims_shopify.domain.payment import OrderMetafield, PaymentIntent, PaymentStatus
from bims_shopify.domain.tenant import PaymentProvider

SHOPIFY = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
STAGING = "https://vpos.infonet.com.py:8888"
CONFIRMATIONS = f"{STAGING}/vpos/api/0.3/single_buy/confirmations"
PRIVATE_KEY = "vpos-private-key-do-not-leak"
ORDER_ID = "18943384158340"
SPID = 4201
NOW = datetime(2026, 10, 20, 14, 30, 5, tzinfo=UTC)

CONFIRMATION = {
    "token": "t" * 32,
    "shop_process_id": SPID,
    "response": "S",
    "response_code": "00",
    "response_description": "Transaccion aprobada",
    "amount": "130.00",
    "currency": "PYG",
    "authorization_number": "123456",
    "ticket_number": "987654321012345",
    "security_information": {"card_source": "L", "risk_index": "0"},
    "extra_attributes": {
        "payment_card_type": "credit",
        "payment_card_brand": "visa",
        "payment_masked_card_number": "411111******1111",
        "payment_origin": "ecommerce",
    },
}


def _field(key: str, type_: str, value: str) -> OrderMetafield:
    return OrderMetafield(namespace="bims_bancard", key=key, type=type_, value=value)


EXPECTED = [
    _field("shop_process_id", "number_integer", "4201"),
    _field("ticket_number", "number_integer", "987654321012345"),
    _field("response_code", "single_line_text_field", "00"),
    _field("authorization_number", "single_line_text_field", "123456"),
    _field("payment_masked_card_number", "single_line_text_field", "411111******1111"),
    _field("payment_card_brand", "single_line_text_field", "visa"),
    _field("payment_card_type", "single_line_text_field", "credit"),
    _field("card_source", "single_line_text_field", "L"),
    _field("confirmed_at", "date_time", "2026-10-20T14:30:05"),
]


# --- domain ------------------------------------------------------------------


def test_namespace_is_single_and_fixed():
    assert METAFIELD_NAMESPACE == "bims_bancard"


def test_confirmation_maps_to_the_expected_typed_metafields():
    assert confirmation_metafields(SPID, CONFIRMATION, NOW) == EXPECTED


def test_authorization_number_is_omitted_when_absent():
    confirmation = {k: v for k, v in CONFIRMATION.items() if k != "authorization_number"}

    keys = [m.key for m in confirmation_metafields(SPID, confirmation, NOW)]

    assert "authorization_number" not in keys
    assert "response_code" in keys


def test_missing_optional_card_attributes_are_skipped_not_invented():
    confirmation = {"response_code": "00", "amount": "130.00", "currency": "PYG"}

    result = confirmation_metafields(SPID, confirmation, NOW)

    assert [m.key for m in result] == ["shop_process_id", "response_code", "confirmed_at"]


def test_card_attributes_are_also_read_when_returned_at_the_top_level():
    confirmation = {
        "response_code": "00",
        "payment_card_brand": "master",
        "payment_card_type": "debit",
        "payment_masked_card_number": "5".ljust(6, "0") + "******" + "4444",
    }

    values = {m.key: m.value for m in confirmation_metafields(SPID, confirmation, NOW)}

    assert values["payment_card_brand"] == "master"
    assert values["payment_card_type"] == "debit"


def test_values_are_single_line_and_bounded():
    confirmation = {"response_code": "00", "payment_card_brand": "vi\nsa  " + "x" * 400}

    brand = next(
        m for m in confirmation_metafields(SPID, confirmation, NOW) if m.key == "payment_card_brand"
    )

    assert "\n" not in brand.value and len(brand.value) <= 255


def test_a_non_numeric_ticket_number_is_skipped():
    confirmation = {**CONFIRMATION, "ticket_number": "not-a-number"}

    assert "ticket_number" not in [m.key for m in confirmation_metafields(SPID, confirmation, NOW)]


def test_no_secret_material_in_any_metafield():
    blob = json.dumps([m.__dict__ for m in confirmation_metafields(SPID, CONFIRMATION, NOW)])

    assert PRIVATE_KEY not in blob and "token" not in blob


# --- provider ----------------------------------------------------------------


@pytest.fixture
def provider():
    settings = Settings(
        checkout_link_secret="s" * 32, public_base_url="https://app.example.com", fernet_key=""
    )
    return BancardVposProvider(settings)


@pytest.fixture
def vpos_tenant(tenant):
    tenant.payment_provider = PaymentProvider.BANCARD_VPOS
    tenant.provider_config = {"public_key": "PUB", "environment": "staging"}
    tenant.bancard_vpos_private_key = PRIVATE_KEY
    return tenant


def _intent() -> PaymentIntent:
    return PaymentIntent(
        tenant_slug="acme", order_id=ORDER_ID, amount=130.0, currency="PYG",
        provider_reference=str(SPID),
    )


def _answer(confirmation: dict) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "confirmation": confirmation})


@respx.mock
async def test_get_confirmation_requests_the_card_attributes(provider, vpos_tenant):
    route = respx.post(CONFIRMATIONS).mock(return_value=_answer(CONFIRMATION))

    await provider.verify_payment(vpos_tenant, _intent())

    body = json.loads(route.calls.last.request.content)
    assert body["extra_response_attributes"] == [
        "payment_card_type",
        "payment_card_brand",
        "payment_masked_card_number",
        "payment_origin",
    ]
    assert PRIVATE_KEY not in json.dumps(body)


@respx.mock
async def test_confirmed_payment_carries_the_metafields(provider, vpos_tenant, monkeypatch):
    monkeypatch.setattr(provider_module, "_utcnow", lambda: NOW)
    respx.post(CONFIRMATIONS).mock(return_value=_answer(CONFIRMATION))
    intent = _intent()

    status = await provider.verify_payment(vpos_tenant, intent)

    assert status == PaymentStatus.CONFIRMED
    assert intent.order_metafields == EXPECTED


@pytest.mark.parametrize("code", ["05", "51", "99"])
@respx.mock
async def test_unconfirmed_payments_carry_no_metafields(provider, vpos_tenant, code):
    respx.post(CONFIRMATIONS).mock(return_value=_answer({**CONFIRMATION, "response_code": code}))
    intent = _intent()

    await provider.verify_payment(vpos_tenant, intent)

    assert intent.order_metafields == []


@respx.mock
async def test_amount_mismatch_carries_no_metafields(provider, vpos_tenant):
    respx.post(CONFIRMATIONS).mock(return_value=_answer({**CONFIRMATION, "amount": "999.00"}))
    intent = _intent()

    assert await provider.verify_payment(vpos_tenant, intent) == PaymentStatus.FAILED
    assert intent.order_metafields == []


# --- Shopify client ----------------------------------------------------------


@respx.mock
async def test_set_order_metafields_sends_exact_metafields_set_variables(tenant):
    route = respx.post(SHOPIFY).mock(
        return_value=httpx.Response(
            200, json={"data": {"metafieldsSet": {"metafields": [], "userErrors": []}}}
        )
    )

    await ShopifyClient(tenant).set_order_metafields(tenant, ORDER_ID, EXPECTED)

    sent = json.loads(route.calls.last.request.content)
    assert "metafieldsSet" in sent["query"] and "userErrors" in sent["query"]
    owner = f"gid://shopify/Order/{ORDER_ID}"
    assert sent["variables"] == {
        "metafields": [
            {"ownerId": owner, "namespace": "bims_bancard", "key": m.key, "type": m.type, "value": m.value}
            for m in EXPECTED
        ]
    }


@respx.mock
async def test_set_order_metafields_accepts_a_gid_and_skips_an_empty_list(tenant):
    route = respx.post(SHOPIFY).mock(
        return_value=httpx.Response(
            200, json={"data": {"metafieldsSet": {"metafields": [], "userErrors": []}}}
        )
    )
    client = ShopifyClient(tenant)

    await client.set_order_metafields(tenant, "gid://shopify/Order/5", [])
    assert not route.called

    await client.set_order_metafields(tenant, "gid://shopify/Order/5", EXPECTED[:1])
    sent = json.loads(route.calls.last.request.content)
    assert sent["variables"]["metafields"][0]["ownerId"] == "gid://shopify/Order/5"


@respx.mock
async def test_set_order_metafields_raises_on_user_errors(tenant):
    from bims_shopify.adapters.shopify.client import ShopifyGraphQLError

    respx.post(SHOPIFY).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"metafieldsSet": {"metafields": None, "userErrors": [
                {"field": ["metafields", "0"], "message": "bad", "code": "INVALID_VALUE"}]}}},
        )
    )

    with pytest.raises(ShopifyGraphQLError):
        await ShopifyClient(tenant).set_order_metafields(tenant, ORDER_ID, EXPECTED)


@respx.mock
async def test_set_order_metafields_batches_at_the_documented_limit_of_25(tenant):
    route = respx.post(SHOPIFY).mock(
        return_value=httpx.Response(
            200, json={"data": {"metafieldsSet": {"metafields": [], "userErrors": []}}}
        )
    )
    many = [_field(f"k{i}", "single_line_text_field", "v") for i in range(30)]

    await ShopifyClient(tenant).set_order_metafields(tenant, ORDER_ID, many)

    sizes = [len(json.loads(c.request.content)["variables"]["metafields"]) for c in route.calls]
    assert sizes == [25, 5]


# --- use case ----------------------------------------------------------------


class _Provider:
    name = "bancard_vpos"

    def __init__(self, metafields: list[OrderMetafield]) -> None:
        self._metafields = metafields

    async def parse_callback(self, tenant, payload):
        return PaymentIntent(
            tenant_slug="acme", order_id=ORDER_ID, amount=130.0, currency="PYG",
            provider_reference=str(SPID),
        )

    async def verify_payment(self, tenant, intent):
        intent.order_metafields = list(self._metafields)
        return PaymentStatus.CONFIRMED


class _Storefront:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.events: list[tuple[str, object]] = []
        self._fail_with = fail_with

    async def mark_order_as_paid(self, tenant, order_id):
        self.events.append(("paid", order_id))

    async def set_order_metafields(self, tenant, order_id, metafields):
        self.events.append(("metafields", list(metafields)))
        if self._fail_with is not None:
            raise self._fail_with


class _Events:
    def __init__(self) -> None:
        self.claimed: set = set()
        self.status: dict = {}

    async def try_claim_event(self, tenant_id, source, external_id, payload_hash):
        if (tenant_id, source, external_id) in self.claimed:
            return False
        self.claimed.add((tenant_id, source, external_id))
        return True

    async def get_event_status(self, tenant_id, source, external_id):
        return self.status.get((tenant_id, source, external_id))

    async def mark_event_status(self, tenant_id, source, external_id, status):
        self.status[(tenant_id, source, external_id)] = status

    async def delete_event(self, tenant_id, source, external_id):
        self.claimed.discard((tenant_id, source, external_id))


class _Audit:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.entries.append(kwargs)


async def test_order_is_marked_paid_first_then_annotated(tenant):
    storefront = _Storefront()
    use_case = HandlePaymentCallback(_Provider(EXPECTED), storefront, _Events())

    intent = await use_case.run(tenant, {})

    assert intent.status == "paid"
    assert storefront.events == [("paid", ORDER_ID), ("metafields", EXPECTED)]


async def test_metafield_failure_keeps_the_order_paid_and_leaves_a_warning_and_audit(tenant):
    from structlog.testing import capture_logs

    storefront = _Storefront(fail_with=RuntimeError("shopify down"))
    audit = _Audit()
    events = _Events()
    use_case = HandlePaymentCallback(_Provider(EXPECTED), storefront, events, audit=audit)

    with capture_logs() as logs:
        intent = await use_case.run(tenant, {})

    assert intent.status == "paid"
    assert ("paid", ORDER_ID) in storefront.events
    assert events.status[(tenant.id, "payment:bancard_vpos", str(SPID))] == "paid"
    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert [e["event"] for e in warnings] == ["payment_metafields_failed"]
    assert warnings[0]["order_id"] == ORDER_ID and warnings[0]["error"] == "RuntimeError"
    assert [e["action"] for e in audit.entries] == ["payment.metafields_failed"]
    assert audit.entries[0]["entity_id"] == ORDER_ID
    assert audit.entries[0]["payload"] == {"error": "RuntimeError", "shop_process_id": str(SPID)}


async def test_a_failing_audit_log_does_not_undo_the_payment(tenant):
    class BrokenAudit:
        async def log(self, **kwargs):
            raise RuntimeError("audit down")

    storefront = _Storefront(fail_with=RuntimeError("shopify down"))
    use_case = HandlePaymentCallback(_Provider(EXPECTED), storefront, _Events(), audit=BrokenAudit())

    assert (await use_case.run(tenant, {})).status == "paid"


async def test_redelivered_callback_writes_the_metafields_only_once(tenant):
    storefront = _Storefront()
    use_case = HandlePaymentCallback(_Provider(EXPECTED), storefront, _Events())

    await use_case.run(tenant, {})
    again = await use_case.run(tenant, {})

    assert again.status == "paid"
    assert [e[0] for e in storefront.events] == ["paid", "metafields"]


async def test_providers_without_metafields_never_touch_the_storefront_method(tenant):
    class NoMetafieldsStorefront:
        def __init__(self):
            self.paid = []

        async def mark_order_as_paid(self, tenant, order_id):
            self.paid.append(order_id)

    storefront = NoMetafieldsStorefront()
    use_case = HandlePaymentCallback(_Provider([]), storefront, _Events())

    assert (await use_case.run(tenant, {})).status == "paid"
    assert storefront.paid == [ORDER_ID]

