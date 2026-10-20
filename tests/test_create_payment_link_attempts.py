"""CreatePaymentLink attempt policy (idempotent while pending/paid, retry on failed/cancelled)."""
from __future__ import annotations

import pytest

from bims_shopify.application.payments import CreatePaymentLink
from bims_shopify.domain.bancard_vpos import VposAttemptsExhaustedError
from bims_shopify.domain.payment import PaymentIntent

ORDER = "1001"


class FakeIntents:
    def __init__(self) -> None:
        self.rows: dict[str, PaymentIntent] = {}

    async def get_by_order_id(self, tenant_id, provider, order_id):
        return self.rows.get(order_id)

    async def save(self, tenant_id, provider, intent):
        self.rows[intent.order_id] = intent


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.entries.append(kwargs)


class AttemptProvider:
    name = "bancard_vpos"
    supports_attempts = True

    def __init__(self, abandon_error: Exception | None = None) -> None:
        self.created: list[int] = []
        self.abandoned: list[str] = []
        self._abandon_error = abandon_error

    async def create_checkout(self, tenant, order_id, amount, currency, *, attempt=0):
        self.created.append(attempt)
        return PaymentIntent(
            tenant_slug=tenant.slug,
            order_id=order_id,
            amount=amount,
            currency=currency,
            checkout_url=f"https://x/pay/{attempt}",
            provider_reference=str(int(order_id) * 100 + attempt),
            process_id=f"pid-{attempt}",
        )

    async def abandon_attempt(self, tenant, intent):
        self.abandoned.append(intent.provider_reference)
        if self._abandon_error:
            raise self._abandon_error


class PlainProvider:
    name = "pagopar"

    def __init__(self) -> None:
        self.calls = 0

    async def create_checkout(self, tenant, order_id, amount, currency):
        self.calls += 1
        return PaymentIntent(
            tenant_slug=tenant.slug, order_id=order_id, amount=amount, currency=currency,
            checkout_url="https://p/1", provider_reference="hash",
        )


def _make(provider, audit=None):
    intents = FakeIntents()
    return CreatePaymentLink(provider, intents, audit=audit), intents


async def test_pending_is_idempotent(tenant):
    provider = AttemptProvider()
    use_case, _ = _make(provider)
    first = await use_case.run(tenant, ORDER, 100.0, "PYG")
    second = await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert provider.created == [0]
    assert second.checkout_url == first.checkout_url


async def test_changed_amount_abandons_the_old_attempt_and_mints_the_next(tenant):
    provider = AttemptProvider()
    audit = FakeAudit()
    use_case, intents = _make(provider, audit)
    await use_case.run(tenant, ORDER, 100.0, "PYG")
    result = await use_case.run(tenant, ORDER, 120.0, "PYG")
    assert provider.created == [0, 1]
    assert provider.abandoned == ["100100"]
    assert result.amount == 120.0
    assert result.process_id == "pid-1"
    assert intents.rows[ORDER].provider_reference == "100101"
    assert [e["action"] for e in audit.entries] == ["payment.retry_minted"]


async def test_same_amount_after_rounding_still_reuses(tenant):
    provider = AttemptProvider()
    use_case, _ = _make(provider)
    await use_case.run(tenant, ORDER, 100.0, "PYG")
    result = await use_case.run(tenant, ORDER, 100.004, "PYG")
    assert provider.created == [0]
    assert result.process_id == "pid-0"


async def test_prepared_intent_without_an_operation_mints_attempt_zero(tenant):
    provider = AttemptProvider()
    use_case, intents = _make(provider)
    intents.rows[ORDER] = PaymentIntent(
        tenant_slug="acme", order_id=ORDER, amount=100.0, currency="PYG",
        checkout_url="https://x/link", provider_reference=None,
    )
    result = await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert provider.created == [0]
    assert provider.abandoned == []
    assert result.process_id == "pid-0"


async def test_legacy_pending_without_process_id_is_replaced(tenant):
    provider = AttemptProvider()
    use_case, intents = _make(provider)
    intents.rows[ORDER] = PaymentIntent(
        tenant_slug="acme", order_id=ORDER, amount=100.0, currency="PYG",
        checkout_url="https://x/old", provider_reference="100100",
    )
    result = await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert provider.created == [1]
    assert provider.abandoned == ["100100"]
    assert result.provider_reference == "100101"


async def test_changed_amount_with_no_attempts_left_raises(tenant):
    provider = AttemptProvider()
    use_case, intents = _make(provider)
    intents.rows[ORDER] = PaymentIntent(
        tenant_slug="acme", order_id=ORDER, amount=100.0, currency="PYG",
        checkout_url="u", provider_reference="100199", process_id="p",
    )
    with pytest.raises(VposAttemptsExhaustedError):
        await use_case.run(tenant, ORDER, 120.0, "PYG")


async def test_paid_never_calls_provider_or_rolls_back(tenant):
    provider = AttemptProvider()
    use_case, intents = _make(provider)
    await use_case.run(tenant, ORDER, 100.0, "PYG")
    intents.rows[ORDER].status = "paid"
    result = await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert result.status == "paid"
    assert provider.created == [0]
    assert provider.abandoned == []


@pytest.mark.parametrize("status", ["cancelled", "failed"])
async def test_failed_or_cancelled_mints_next_attempt(tenant, status):
    provider = AttemptProvider()
    audit = FakeAudit()
    use_case, intents = _make(provider, audit)
    await use_case.run(tenant, ORDER, 100.0, "PYG")
    intents.rows[ORDER].status = status
    result = await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert provider.created == [0, 1]
    assert provider.abandoned == ["100100"]
    assert result.provider_reference == "100101"
    assert intents.rows[ORDER].provider_reference == "100101"
    assert intents.rows[ORDER].status == "pending"
    assert [e["action"] for e in audit.entries] == ["payment.retry_minted"]
    assert audit.entries[0]["payload"]["attempt"] == 1


async def test_abandon_failure_does_not_block_retry(tenant):
    provider = AttemptProvider(abandon_error=RuntimeError("boom"))
    use_case, intents = _make(provider)
    await use_case.run(tenant, ORDER, 100.0, "PYG")
    intents.rows[ORDER].status = "failed"
    result = await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert result.provider_reference == "100101"


async def test_pending_without_checkout_url_retries_same_attempt(tenant):
    provider = AttemptProvider()
    use_case, intents = _make(provider)
    intents.rows[ORDER] = PaymentIntent(
        tenant_slug="acme", order_id=ORDER, amount=1.0, currency="PYG",
        checkout_url=None, provider_reference="100102",
    )
    result = await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert provider.created == [2]
    assert provider.abandoned == []
    assert result.provider_reference == "100102"


async def test_attempts_exhausted(tenant):
    provider = AttemptProvider()
    use_case, intents = _make(provider)
    intents.rows[ORDER] = PaymentIntent(
        tenant_slug="acme", order_id=ORDER, amount=1.0, currency="PYG",
        checkout_url="u", provider_reference="100199", status="failed",
    )
    with pytest.raises(VposAttemptsExhaustedError):
        await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert provider.created == []


async def test_providers_without_attempts_keep_legacy_behaviour(tenant):
    provider = PlainProvider()
    use_case, intents = _make(provider)
    await use_case.run(tenant, ORDER, 100.0, "PYG")
    intents.rows[ORDER].status = "failed"
    await use_case.run(tenant, ORDER, 100.0, "PYG")
    assert provider.calls == 1
