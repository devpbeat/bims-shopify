"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        id=1,
        slug="acme",
        bims_base_url="https://bims.example.com",
        bims_api_key="acme_secretkey123",
        shopify_shop_domain="acme.myshopify.com",
        shopify_access_token="shpat_test",
        shopify_webhook_secret="whsecret",
        shopify_location_id="gid://shopify/Location/1",
        bims_posale_id=1,
        bims_warehouse_id=1,
        bims_company_id=1,
        bims_currency_id=1,
        bims_payment_method_id=1,
        default_customer_contact_id=1,
        reorder_threshold=5.0,
        reorder_strategy=ReorderStrategy.PURCHASE_ORDER,
        payment_provider=PaymentProvider.BANCARD,
        provider_config={},
        field_mappings={},
        active=True,
    )
