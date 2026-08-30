"""Tests for ProcessShopifyOrder handling the three BIMS sales/add.json response shapes."""
import pytest

from bims_shopify.application.process_shopify_order import (
    MaxRetriesExceededError,
    ProcessShopifyOrder,
)
from bims_shopify.domain.sale import SaleOrder, SaleResult, SaleResultKind


class FakeERP:
    def __init__(self, responses: list[SaleResult], verify_response: dict | None = None):
        self._responses = list(responses)
        self.verify_response = verify_response or {"data": {"already_synced": False}}
        self.create_calls = 0
        self.verify_calls = 0

    async def create_or_update_sale(self, tenant, sale):
        self.create_calls += 1
        return self._responses.pop(0)

    async def verify_sale_synced(self, tenant, sale):
        self.verify_calls += 1
        return self.verify_response

    async def list_products(self, tenant):
        raise NotImplementedError

    async def create_purchase_order(self, tenant, contact_id, lines):
        raise NotImplementedError

    async def request_restocking(self, tenant, payload):
        raise NotImplementedError

    async def update_stock(self, tenant, payload):
        raise NotImplementedError


def _sale(tenant) -> SaleOrder:
    return SaleOrder(
        external_id="shopify-order-1",
        contact_id=tenant.default_customer_contact_id,
        posale_id=tenant.bims_posale_id,
        agency_id=tenant.bims_warehouse_id,
        company_id=tenant.bims_company_id,
        currency_id=tenant.bims_currency_id,
        amount=100.0,
        invoice_number="0001-001-0000001",
    )


async def test_success_response_returns_immediately(tenant):
    erp = FakeERP([SaleResult(kind=SaleResultKind.SUCCESS, data={"Sale": {"id": 1}})])
    use_case = ProcessShopifyOrder(erp)
    result = await use_case.run(tenant, _sale(tenant))
    assert result.kind == SaleResultKind.SUCCESS
    assert erp.create_calls == 1
    assert erp.verify_calls == 0


async def test_deadlock_retryable_retries_then_succeeds(tenant):
    erp = FakeERP(
        [
            SaleResult(kind=SaleResultKind.DEADLOCK_RETRYABLE, retry_after_ms=0),
            SaleResult(kind=SaleResultKind.SUCCESS, data={"Sale": {"id": 1}}),
        ]
    )
    use_case = ProcessShopifyOrder(erp, max_retries=3)
    result = await use_case.run(tenant, _sale(tenant))
    assert result.kind == SaleResultKind.SUCCESS
    assert erp.create_calls == 2


async def test_deadlock_retryable_exhausts_retries(tenant):
    erp = FakeERP(
        [SaleResult(kind=SaleResultKind.DEADLOCK_RETRYABLE, retry_after_ms=0) for _ in range(3)]
    )
    use_case = ProcessShopifyOrder(erp, max_retries=3)
    with pytest.raises(MaxRetriesExceededError):
        await use_case.run(tenant, _sale(tenant))


async def test_unconfirmed_then_verify_reports_already_synced(tenant):
    erp = FakeERP(
        [SaleResult(kind=SaleResultKind.UNCONFIRMED, message="unknown outcome")],
        verify_response={"data": {"already_synced": True, "Sale": {"id": 42}}},
    )
    use_case = ProcessShopifyOrder(erp)
    result = await use_case.run(tenant, _sale(tenant))
    assert result.kind == SaleResultKind.SUCCESS
    assert result.data == {"id": 42}
    assert erp.verify_calls == 1


async def test_unconfirmed_not_synced_retries_then_exhausts(tenant):
    erp = FakeERP(
        [SaleResult(kind=SaleResultKind.UNCONFIRMED) for _ in range(2)],
        verify_response={"data": {"already_synced": False}},
    )
    use_case = ProcessShopifyOrder(erp, max_retries=2)
    with pytest.raises(MaxRetriesExceededError):
        await use_case.run(tenant, _sale(tenant))
    assert erp.create_calls == 2
    assert erp.verify_calls == 2
