"""RollbackPayment use case: resolution, status policy, audit, idempotency."""
from __future__ import annotations

import json

import pytest
import structlog

from bims_shopify.application import payment_rollback
from bims_shopify.application.payment_rollback import (
    PaymentIntentNotFoundError,
    RollbackInputError,
    RollbackNotApplicableError,
    RollbackPayment,
)
from bims_shopify.domain.bancard_vpos import (
    RollbackResult,
    RollbackVerdict,
    VposConfigurationError,
)
from bims_shopify.domain.payment import PaymentIntent


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.entries.append(kwargs)


class FakeIntents:
    def __init__(self, *intents: PaymentIntent) -> None:
        self.rows = {i.order_id: i for i in intents}

    async def get_by_order_id(self, tenant_id, provider, order_id):
        return self.rows.get(order_id)

    async def get_by_provider_reference(self, tenant_id, provider, ref):
        return next((i for i in self.rows.values() if i.provider_reference == ref), None)

    async def update_status(self, tenant_id, provider, order_id, status):
        self.rows[order_id].status = status


class FakeProvider:
    name = "bancard_vpos"

    def __init__(self, *results: RollbackResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[PaymentIntent] = []

    async def rollback_operation(self, tenant, intent):
        self.calls.append(intent)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _intent(order="900", spid="90000", status="paid") -> PaymentIntent:
    return PaymentIntent(
        tenant_slug="acme", order_id=order, amount=130.0, currency="PYG",
        provider_reference=spid, status=status,
    )


def _case(*results, intent=None):
    intent = intent or _intent()
    intents, audit = FakeIntents(intent), FakeAudit()
    provider = FakeProvider(*results)
    return RollbackPayment(provider, intents, audit), provider, intents, audit, intent


def _ok(verdict, key):
    return RollbackResult(verdict, key)


async def test_success_cancels_the_local_intent_and_audits(tenant):
    use_case, _, _, audit, intent = _case(_ok(RollbackVerdict.SUCCEEDED, "RollbackSuccessful"))

    report = await use_case.run(tenant, order_id="900", shop_process_id=None, actor="portal")

    assert report.verdict is RollbackVerdict.SUCCEEDED
    assert report.local_status == "cancelled" and intent.status == "cancelled"
    (entry,) = audit.entries
    assert entry["action"] == "payment.rollback_requested" and entry["actor"] == "portal"
    assert entry["tenant_id"] == tenant.id
    assert entry["payload"] == {
        "order_id": "900", "shop_process_id": "90000", "outcome": "succeeded",
        "key": "RollbackSuccessful", "status_before": "paid", "status_after": "cancelled",
    }


async def test_second_rollback_is_idempotent(tenant):
    use_case, _, _, audit, intent = _case(
        _ok(RollbackVerdict.SUCCEEDED, "RollbackSuccessful"),
        _ok(RollbackVerdict.NO_OP, "AlreadyRollbackedError"),
    )

    await use_case.run(tenant, order_id="900", shop_process_id=None)
    second = await use_case.run(tenant, order_id="900", shop_process_id=None)

    assert second.verdict is RollbackVerdict.NO_OP and intent.status == "cancelled"
    assert len(audit.entries) == 2


async def test_payment_not_found_leaves_the_status_alone(tenant):
    use_case, _, _, _, intent = _case(
        _ok(RollbackVerdict.NO_OP, "PaymentNotFoundError"), intent=_intent(status="pending")
    )

    report = await use_case.run(tenant, order_id=None, shop_process_id="90000")

    assert report.local_status == "pending" and intent.status == "pending"


@pytest.mark.parametrize(
    "result",
    [
        _ok(RollbackVerdict.MANUAL_ANNULMENT_REQUIRED, "TransactionAlreadyConfirmed"),
        _ok(RollbackVerdict.UNEXPECTED, "SomethingNew"),
        RollbackResult(RollbackVerdict.TRANSPORT_FAILURE),
    ],
)
async def test_non_reversals_never_touch_the_local_status(tenant, result):
    use_case, _, _, audit, intent = _case(result)

    report = await use_case.run(tenant, order_id="900", shop_process_id=None)

    assert report.verdict is result.verdict and intent.status == "paid"
    assert audit.entries[0]["payload"]["status_after"] == "paid"


async def test_manual_annulment_is_logged_at_error_level_and_audited_twice(tenant, monkeypatch):
    # The module logger may already be cached by an earlier test; a fresh one is capturable.
    monkeypatch.setattr(payment_rollback, "logger", structlog.get_logger("rollback-test"))
    use_case, _, _, audit, _ = _case(
        _ok(RollbackVerdict.MANUAL_ANNULMENT_REQUIRED, "TransactionAlreadyConfirmed")
    )

    with structlog.testing.capture_logs() as logs:
        await use_case.run(tenant, order_id="900", shop_process_id=None)

    errors = [e for e in logs if e["log_level"] == "error"]
    assert len(errors) == 1 and errors[0]["shop_process_id"] == "90000"
    assert [e["action"] for e in audit.entries] == [
        "payment.rollback_conflict", "payment.rollback_requested",
    ]


@pytest.mark.parametrize(
    ("order_id", "spid", "reason"),
    [
        (None, None, "missing_identifier"),
        ("", "  ", "missing_identifier"),
        ("900", "91111", "ambiguous"),
        (None, "abc", "invalid_shop_process_id"),
    ],
)
async def test_bad_input_is_rejected_audited_and_never_hits_bancard(tenant, order_id, spid, reason):
    intents = FakeIntents(_intent(), _intent(order="901", spid="91111"))
    audit, provider = FakeAudit(), FakeProvider()
    use_case = RollbackPayment(provider, intents, audit)

    with pytest.raises(RollbackInputError) as caught:
        await use_case.run(tenant, order_id=order_id, shop_process_id=spid)

    assert caught.value.reason == reason and provider.calls == []
    assert audit.entries[0]["payload"]["outcome"] == f"rejected_{reason}"


async def test_matching_order_and_reference_is_not_ambiguous(tenant):
    use_case, provider, *_ = _case(_ok(RollbackVerdict.SUCCEEDED, "RollbackSuccessful"))

    await use_case.run(tenant, order_id="900", shop_process_id="90000")

    assert len(provider.calls) == 1


async def test_gid_order_ids_are_normalised(tenant):
    use_case, provider, *_ = _case(_ok(RollbackVerdict.SUCCEEDED, "RollbackSuccessful"))

    await use_case.run(tenant, order_id="gid://shopify/Order/900", shop_process_id=None)

    assert provider.calls[0].order_id == "900"


async def test_unknown_payment_is_not_found_and_audited(tenant):
    use_case, provider, _, audit, _ = _case()

    with pytest.raises(PaymentIntentNotFoundError):
        await use_case.run(tenant, order_id="404", shop_process_id=None)

    assert provider.calls == [] and audit.entries[0]["payload"]["outcome"] == "not_found"


async def test_placeholder_intent_without_an_operation_is_not_rollbackable(tenant):
    placeholder = _intent(spid=None, status="pending")
    use_case, provider, _, audit, _ = _case(intent=placeholder)

    with pytest.raises(RollbackNotApplicableError):
        await use_case.run(tenant, order_id="900", shop_process_id=None)

    assert provider.calls == [] and audit.entries[0]["payload"]["outcome"] == "no_operation"


async def test_configuration_error_propagates_after_being_audited(tenant):
    use_case, _, _, audit, intent = _case(VposConfigurationError("rejected"))

    with pytest.raises(VposConfigurationError):
        await use_case.run(tenant, order_id="900", shop_process_id=None)

    assert intent.status == "paid"
    assert audit.entries[0]["payload"]["outcome"] == "configuration_error"
    assert "rejected" not in json.dumps(audit.entries)

async def test_already_rollbacked_on_a_paid_intent_reconciles_it_to_cancelled(tenant):
    use_case, _, _, _, intent = _case(_ok(RollbackVerdict.NO_OP, "AlreadyRollbackedError"))

    report = await use_case.run(tenant, order_id="900", shop_process_id=None)

    assert report.local_status == "cancelled" and intent.status == "cancelled"
