"""Unit tests for translating a Shopify order webhook payload into a BIMS SaleOrder.

Regression coverage for PYG (Paraguayan Guarani) amount rounding: PYG has no
fractional subunit, but Shopify always formats money fields with two
decimals, so a naive float pass-through can hand BIMS a fractional Guarani
amount, producing a malformed sale.
"""
from __future__ import annotations

from bims_shopify.api.webhooks import build_sale_from_webhook_payload


def _payload(**overrides):
    base = {
        "id": 123456,
        "total_price": "150000.50",
        "line_items": [
            {"product_id": 1, "quantity": 2, "price": "75000.25"},
        ],
    }
    base.update(overrides)
    return base


def test_amount_is_rounded_to_whole_pyg(tenant):
    sale = build_sale_from_webhook_payload(tenant, _payload())

    assert sale.amount == 150000
    assert isinstance(sale.amount, int)
    assert sale.payments[0].amount == 150000


def test_line_item_price_is_rounded_to_whole_pyg(tenant):
    sale = build_sale_from_webhook_payload(tenant, _payload())

    assert sale.line_items[0].price == 75000
    assert isinstance(sale.line_items[0].price, int)


def test_exact_whole_amount_is_unaffected(tenant):
    sale = build_sale_from_webhook_payload(tenant, _payload(total_price="90000.00"))

    assert sale.amount == 90000


def test_external_id_used_as_idempotency_key(tenant):
    sale = build_sale_from_webhook_payload(tenant, _payload(id=987))

    assert sale.external_id == "987"
