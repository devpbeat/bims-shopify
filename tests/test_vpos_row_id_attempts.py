"""CreatePaymentLink derives the vPOS shop_process_id from the intent row id."""
from __future__ import annotations

import pytest

from bims_shopify.application.payments import CreatePaymentLink
from bims_shopify.domain.bancard_vpos import (
    VposAttemptsExhaustedError,
    build_shop_process_id,
)
from bims_shopify.domain.payment import PaymentIntent

REAL_ORDER_ID = "18943384158340"  # 14 digits, as seen in production


class FakeIntents:
    """Assigns sequential row ids like the real table."""

    def __init__(self) -> None:
        self.rows: dict[str, PaymentIntent] = {}
        self._next = 1
        self._ids: dict[str, int] = {}

    async def get_by_order_id(self, tenant_id, provider, order_id):
        row = self.rows.get(order_id)
        return None if row is None else PaymentIntent(**{**row.__dict__})

    async def save(self, tenant_id, provider, intent):
        if intent.order_id not in self._ids:
            self._ids[intent.order_id] = self._next
            self._next += 1
        intent.row_id = self._ids[intent.order_id]
        self.rows[intent.order_id] = PaymentIntent(**{**intent.__dict__})


class FakeVpos:
    name = "bancard_vpos"
    supports_attempts = True
    defers_checkout = True

    def __init__(self) -> None:
        self.minted: list[tuple[int, int]] = []
        self.abandoned: list[str | None] = []

    def prepare_link(self, tenant, order_id, amount, currency):
        return PaymentIntent(
            tenant_slug="acme", order_id=order_id, amount=amount, currency=currency,
            checkout_url="https://pay.example/x",
        )

    async def create_checkout(self, tenant, order_id, amount, currency, *, row_id, attempt=0):
        spid = build_shop_process_id(row_id, attempt)
        self.minted.append((row_id, attempt))
        return PaymentIntent(
            tenant_slug="acme", order_id=order_id, amount=amount, currency=currency,
            checkout_url="https://pay.example/x", provider_reference=str(spid),
            process_id=f"p{spid}",
        )

    async def abandon_attempt(self, tenant, intent):
        self.abandoned.append(intent.provider_reference)


class _Tenant:
    id = 1
    slug = "acme"


@pytest.mark.parametrize("order_id", [REAL_ORDER_ID, "189433841583401"])
async def test_large_order_ids_mint_a_short_row_based_id(order_id):
    intents, provider = FakeIntents(), FakeVpos()

    intent = await CreatePaymentLink(provider, intents).run(_Tenant(), order_id, 130.0, "PYG")

    assert intent.provider_reference == "100"
    assert len(intent.provider_reference) <= 15
    assert provider.minted == [(1, 0)]


async def test_row_created_before_minting_when_no_webhook_ever_arrived():
    intents, provider = FakeIntents(), FakeVpos()
    assert intents.rows == {}

    await CreatePaymentLink(provider, intents).run(_Tenant(), REAL_ORDER_ID, 130.0, "PYG")

    assert intents.rows[REAL_ORDER_ID].row_id == 1
    assert provider.minted[0][0] == 1


async def test_existing_placeholder_row_id_is_reused_not_duplicated():
    intents, provider = FakeIntents(), FakeVpos()
    use_case = CreatePaymentLink(provider, intents)
    await use_case.prepare(_Tenant(), REAL_ORDER_ID, 130.0, "PYG")
    await use_case.prepare(_Tenant(), "555", 10.0, "PYG")

    await use_case.run(_Tenant(), "555", 10.0, "PYG")

    assert provider.minted == [(2, 0)]


async def test_second_attempt_keeps_row_id_and_changes_the_id():
    intents, provider = FakeIntents(), FakeVpos()
    use_case = CreatePaymentLink(provider, intents)
    first = await use_case.run(_Tenant(), REAL_ORDER_ID, 130.0, "PYG")
    intents.rows[REAL_ORDER_ID].status = "failed"

    second = await use_case.run(_Tenant(), REAL_ORDER_ID, 130.0, "PYG")

    assert (first.provider_reference, second.provider_reference) == ("100", "101")
    assert provider.minted == [(1, 0), (1, 1)]
    assert provider.abandoned == ["100"]


async def test_attempt_99_is_exhausted():
    intents, provider = FakeIntents(), FakeVpos()
    use_case = CreatePaymentLink(provider, intents)
    await use_case.run(_Tenant(), REAL_ORDER_ID, 130.0, "PYG")
    row = intents.rows[REAL_ORDER_ID]
    row.provider_reference = "199"
    row.status = "failed"

    with pytest.raises(VposAttemptsExhaustedError):
        await use_case.run(_Tenant(), REAL_ORDER_ID, 130.0, "PYG")
