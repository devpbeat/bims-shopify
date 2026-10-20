"""App-level webhooks must resolve the tenant by its stored shopify_shop_domain.

Regression: the handler inferred slug from the shop subdomain, silently
ignoring tenants whose slug differs from it (e.g. slug `mystore`, domain
`1ncq0u-03.myshopify.com`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import webhooks

API_SECRET = "app-client-secret"
PROD_DOMAIN = "1ncq0u-03.myshopify.com"


@pytest.fixture
def app_env(vpos_app):
    app, settings, make_tenant = vpos_app
    settings.shopify_api_secret = API_SECRET
    app.include_router(webhooks.router)

    async def make(**overrides):
        return await make_tenant(**{"push_orders_to_bims": False, **overrides})

    return app, settings, make


async def _post(app, path, payload, shop=None, topic="orders/create") -> httpx.Response:
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(API_SECRET.encode(), body, hashlib.sha256).digest()).decode()
    headers = {"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": topic}
    if shop is not None:
        headers["X-Shopify-Shop-Domain"] = shop
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, content=body, headers=headers)


def _order():
    return {"id": 1001, "total_price": "130.00", "currency": "PYG", "payment_gateway_names": ["Bancard"]}


async def _intent(app, tenant_id):
    async with app.state.session_factory() as session:
        return await SqlAlchemyPaymentIntentRepository(session).get_by_order_id(
            tenant_id, "bancard_vpos", "1001"
        )


@respx.mock
async def test_slug_differing_from_domain_subdomain_is_resolved_and_bridge_runs(app_env):
    app, _, make = app_env
    tenant = await make(slug="mystore", shopify_shop_domain=PROD_DOMAIN)

    with capture_logs() as logs:
        resp = await _post(app, "/webhooks/shopify/app", _order(), shop=PROD_DOMAIN)

    assert resp.json() == {"status": "accepted"}
    assert not any(e["event"] == "app_webhook_unknown_shop" for e in logs)
    assert not any(e["event"] == "app_webhook_slug_fallback" for e in logs)
    assert (await _intent(app, tenant.id)) is not None


@pytest.mark.parametrize("header", [PROD_DOMAIN.upper(), f"  {PROD_DOMAIN}  ", f"\t{PROD_DOMAIN.title()}\n"])
async def test_header_case_and_whitespace_variations_resolve(app_env, header):
    app, _, make = app_env
    tenant = await make(slug="mystore", shopify_shop_domain=PROD_DOMAIN)

    resp = await _post(app, "/webhooks/shopify/app", _order(), shop=header)

    assert resp.json() == {"status": "accepted"}
    assert (await _intent(app, tenant.id)) is not None


async def test_slug_equals_subdomain_fallback_still_resolves_and_is_logged(app_env):
    app, _, make = app_env
    # Stored domain is stale/missing, but slug matches the subdomain.
    tenant = await make(slug="acme", shopify_shop_domain="old-name.myshopify.com")

    with capture_logs() as logs:
        resp = await _post(app, "/webhooks/shopify/app", _order(), shop="acme.myshopify.com")

    assert resp.json() == {"status": "accepted"}
    assert any(e["event"] == "app_webhook_slug_fallback" for e in logs)
    assert (await _intent(app, tenant.id)) is not None


async def test_unknown_shop_is_ignored_with_200_and_logged(app_env):
    app, _, make = app_env
    await make(slug="mystore", shopify_shop_domain=PROD_DOMAIN)

    with capture_logs() as logs:
        resp = await _post(app, "/webhooks/shopify/app", _order(), shop="nobody.myshopify.com")

    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert any(e["event"] == "app_webhook_unknown_shop" for e in logs)


async def test_compliance_shop_redact_resolves_by_stored_domain(app_env):
    app, settings, make = app_env
    await make(slug="mystore", shopify_shop_domain=PROD_DOMAIN)

    resp = await _post(
        app, "/webhooks/shopify/compliance/shop/redact", {"shop_domain": PROD_DOMAIN}, topic="shop/redact"
    )

    assert resp.status_code == 200
    async with app.state.session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        assert await repo.get_by_slug("mystore") is None


async def test_compliance_shop_redact_never_deletes_a_tenant_matched_only_by_slug(app_env):
    app, settings, make = app_env
    await make(slug="mystore", shopify_shop_domain=PROD_DOMAIN)

    # A different shop whose subdomain happens to equal another tenant's slug.
    resp = await _post(
        app, "/webhooks/shopify/compliance/shop/redact", {"shop_domain": "mystore.myshopify.com"},
        topic="shop/redact",
    )

    assert resp.status_code == 200
    async with app.state.session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        assert await repo.get_by_slug("mystore") is not None
