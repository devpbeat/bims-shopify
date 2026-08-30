"""bims_timezone must be a valid IANA zoneinfo key, validated at the API schema layer."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from bims_shopify.api.tenants import TenantCreate


def _base_payload(**overrides):
    payload = {
        "slug": "acme",
        "bims_base_url": "https://bims.example.com",
        "bims_api_key": "acme_key",
        "shopify_shop_domain": "acme.myshopify.com",
        "shopify_access_token": "shpat_test",
        "shopify_webhook_secret": "whsecret",
    }
    payload.update(overrides)
    return payload


def test_bims_timezone_defaults_to_america_asuncion():
    tenant = TenantCreate(**_base_payload())
    assert tenant.bims_timezone == "America/Asuncion"


def test_bims_timezone_accepts_valid_iana_zone():
    tenant = TenantCreate(**_base_payload(bims_timezone="America/New_York"))
    assert tenant.bims_timezone == "America/New_York"


def test_bims_timezone_rejects_invalid_zone():
    with pytest.raises(ValidationError, match="Unknown IANA timezone"):
        TenantCreate(**_base_payload(bims_timezone="Not/AZone"))
