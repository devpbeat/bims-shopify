"""Integration-style tests for BIMSClient against a mocked BIMS server via respx."""
import httpx
import pytest
import respx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.erp_adapter import parse_sale_response
from bims_shopify.domain.sale import SaleResultKind


@pytest.fixture
def bims_client(tenant):
    http_client = httpx.AsyncClient(
        base_url=tenant.bims_base_url,
        headers={"Authorization": tenant.bims_auth_header},
    )
    client = BIMSClient(tenant, http_client=http_client)
    yield client


@respx.mock
async def test_add_sale_as_json_sends_no_data_wrapper(bims_client, tenant):
    route = respx.post(f"{tenant.bims_base_url}/api/sales/add.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "code": "200", "data": {"Sale": {"id": 1}}})
    )
    response = await bims_client.add_sale({"Sale": {"id": 1}}, as_json=True)
    assert route.called
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/json"
    assert response["data"]["Sale"]["id"] == 1


@respx.mock
async def test_verify_sale_synced_sends_form_encoded_data_wrapper(bims_client, tenant):
    route = respx.post(f"{tenant.bims_base_url}/api/sales/verify_synced.json").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "code": "200", "data": {"already_synced": True}}
        )
    )
    await bims_client.verify_sale_synced({"invoice_number": "0001-001-01", "billed": True})
    request = route.calls.last.request
    body = request.content.decode("utf-8")
    assert "data%5BSale%5D%5Binvoice_number%5D=0001-001-01" in body
    assert "data%5BSale%5D%5Bbilled%5D=true" in body


def test_parse_sale_response_success():
    result = parse_sale_response({"status": "ok", "code": "200", "data": {"Sale": {"id": 1}}})
    assert result.kind == SaleResultKind.SUCCESS


def test_parse_sale_response_deadlock_retryable():
    result = parse_sale_response(
        {"status": "error", "code": "409", "retryable": True, "retry_after_ms": 200, "message": "deadlock"}
    )
    assert result.kind == SaleResultKind.DEADLOCK_RETRYABLE
    assert result.retry_after_ms == 200


def test_parse_sale_response_unconfirmed():
    result = parse_sale_response(
        {"status": "error", "code": "500", "retryable": False, "retry_after_ms": None, "message": "unknown"}
    )
    assert result.kind == SaleResultKind.UNCONFIRMED
