"""Tests for HandlePaymentCallback: verified-status trust and idempotency."""
from __future__ import annotations

from bims_shopify.application.payments import HandlePaymentCallback
from bims_shopify.domain.payment import PaymentIntent, PaymentStatus


class FakeProvider:
    name = "fakepay"

    def __init__(self, verify_status: PaymentStatus, callback_status: str = "paid") -> None:
        self.verify_status = verify_status
        self.callback_status = callback_status
        self.parse_calls = 0
        self.verify_calls = 0

    async def create_checkout(self, tenant, order_id, amount, currency):
        raise NotImplementedError

    async def parse_callback(self, tenant, payload) -> PaymentIntent:
        self.parse_calls += 1
        return PaymentIntent(
            tenant_slug=tenant.slug,
            order_id=str(payload.get("order_id", "")),
            amount=float(payload.get("amount", 0)),
            currency=payload.get("currency", "PYG"),
            provider_reference=str(payload.get("transaction_id", "")),
            status=self.callback_status,
        )

    async def verify_payment(self, tenant, intent) -> PaymentStatus:
        self.verify_calls += 1
        return self.verify_status


class FakeStorefront:
    def __init__(self) -> None:
        self.marked_paid: list[str] = []

    async def mark_order_as_paid(self, tenant, order_id: str) -> None:
        self.marked_paid.append(order_id)


class FakeProcessedEvents:
    def __init__(self) -> None:
        self._events: dict[tuple, str] = {}

    async def has_processed_event(self, tenant_id, source, external_id) -> bool:
        return (tenant_id, source, external_id) in self._events

    async def get_event_status(self, tenant_id, source, external_id) -> str | None:
        return self._events.get((tenant_id, source, external_id))

    async def record_processed_event(self, tenant_id, source, external_id, payload_hash, status) -> None:
        self._events[(tenant_id, source, external_id)] = status

    async def try_claim_event(self, tenant_id, source, external_id, payload_hash) -> bool:
        key = (tenant_id, source, external_id)
        if key in self._events:
            return False
        self._events[key] = "processing"
        return True

    async def mark_event_status(self, tenant_id, source, external_id, status) -> None:
        self._events[(tenant_id, source, external_id)] = status

    async def delete_event(self, tenant_id, source, external_id) -> None:
        self._events.pop((tenant_id, source, external_id), None)


def _payload() -> dict:
    return {
        "order_id": "1001",
        "amount": 100.0,
        "currency": "PYG",
        "transaction_id": "tx-1",
        "status": "success",
    }


async def test_confirmed_verification_marks_order_paid(tenant):
    provider = FakeProvider(PaymentStatus.CONFIRMED)
    storefront = FakeStorefront()
    use_case = HandlePaymentCallback(provider, storefront, FakeProcessedEvents())

    intent = await use_case.run(tenant, _payload())

    assert intent.status == "paid"
    assert storefront.marked_paid == ["1001"]
    assert provider.verify_calls == 1


async def test_pending_verification_does_not_mark_order_paid(tenant):
    """A spoofed 'success' callback body must not mark the order paid unless verify_payment confirms it."""
    provider = FakeProvider(PaymentStatus.PENDING)
    storefront = FakeStorefront()
    use_case = HandlePaymentCallback(provider, storefront, FakeProcessedEvents())

    intent = await use_case.run(tenant, _payload())

    assert intent.status == "pending"
    assert storefront.marked_paid == []
    assert provider.verify_calls == 1


async def test_failed_verification_does_not_mark_order_paid(tenant):
    provider = FakeProvider(PaymentStatus.FAILED)
    storefront = FakeStorefront()
    use_case = HandlePaymentCallback(provider, storefront, FakeProcessedEvents())

    intent = await use_case.run(tenant, _payload())

    assert intent.status == "failed"
    assert storefront.marked_paid == []


async def test_duplicate_callback_does_not_mark_order_paid_twice(tenant):
    provider = FakeProvider(PaymentStatus.CONFIRMED)
    storefront = FakeStorefront()
    processed_events = FakeProcessedEvents()
    use_case = HandlePaymentCallback(provider, storefront, processed_events)

    first = await use_case.run(tenant, _payload())
    second = await use_case.run(tenant, _payload())

    assert first.status == "paid"
    assert second.status == "paid"
    assert storefront.marked_paid == ["1001"]
    assert provider.verify_calls == 1
    assert provider.parse_calls == 2
