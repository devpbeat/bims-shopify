"""Buyer pay-link endpoint called by the checkout UI extension (Thank you page)."""
from __future__ import annotations

import time

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.domain.payment import PaymentIntent
from bims_shopify.domain.tenant import PaymentProvider

SECRET = "app-client-secret-with-more-than-32-bytes"
CLIENT_ID = "client-id-123"
PATH = "/checkout-extension/pay-link"
PAY_URL = "https://pay.example.com/pay/bancard_vpos/acme/signed-token"
NEUTRAL = {"status": "none"}


def _token(
    *,
    shop: str = "acme.myshopify.com",
    secret: str = SECRET,
    aud: str = CLIENT_ID,
    exp_in: int = 300,
    iss_shop: str | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://{iss_shop or shop}/admin",
        "dest": f"https://{shop}",
        "aud": aud,
        "sub": "42",
        "exp": now + exp_in,
        "nbf": now - 5,
        "iat": now - 5,
        "jti": "f8912129-1af6-4cad-9ca3-76b0f7621087",
        "sid": "abc",
    }
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
async def ext_app(vpos_app):
    from bims_shopify.api import checkout_extension

    app, settings, make_tenant = vpos_app
    settings = settings.model_copy(update={"shopify_api_key": CLIENT_ID, "shopify_api_secret": SECRET})
    app.state.settings = settings
    app.include_router(checkout_extension.router)

    async def seed(tenant, order_id="1001", *, status="pending", url=PAY_URL, provider="bancard_vpos"):
        async with app.state.session_factory() as session:
            await SqlAlchemyPaymentIntentRepository(session).save(
                tenant.id,
                provider,
                PaymentIntent(
                    tenant_slug=tenant.slug,
                    order_id=order_id,
                    amount=1.0,
                    currency="PYG",
                    checkout_url=url,
                    status=status,
                ),
            )

    return app, make_tenant, seed


async def _post(app, token: str | None, order_id="gid://shopify/Order/1001"):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(PATH, json={"order_id": order_id}, headers=headers)


async def test_valid_token_returns_pay_url_for_pending_vpos_order(ext_app):
    app, make_tenant, seed = ext_app
    tenant = await make_tenant()
    await seed(tenant)

    resp = await _post(app, _token())

    assert resp.status_code == 200
    assert resp.json() == {"status": "pay", "pay_url": PAY_URL}
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["cache-control"] == "no-store"


async def test_numeric_order_id_is_accepted(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant())

    resp = await _post(app, _token(), order_id="1001")

    assert resp.json()["status"] == "pay"


async def test_slug_differing_from_domain_subdomain_resolves_by_stored_domain(ext_app):
    app, make_tenant, seed = ext_app
    tenant = await make_tenant(slug="mystore", shopify_shop_domain="1ncq0u-03.myshopify.com")
    await seed(tenant)

    resp = await _post(app, _token(shop="1ncq0u-03.myshopify.com"))

    assert resp.json() == {"status": "pay", "pay_url": PAY_URL}


async def test_stored_domain_is_matched_case_and_whitespace_insensitively(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant())

    resp = await _post(app, _token(shop="ACME.myshopify.com"))

    assert resp.json()["status"] == "pay"


@pytest.mark.parametrize(
    "token_kwargs",
    [{"secret": "some-other-secret-with-more-than-32-bytes"}, {"exp_in": -60}, {"aud": "someone-else"}],
    ids=["wrong-secret", "expired", "wrong-audience"],
)
async def test_bad_tokens_are_rejected(ext_app, token_kwargs):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant())

    resp = await _post(app, _token(**token_kwargs))

    assert resp.status_code == 401
    assert "pay_url" not in resp.text
    assert resp.headers["access-control-allow-origin"] == "*"


async def test_tampered_token_is_rejected(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant())
    header, _payload, signature = _token().split(".")
    forged = _token(shop="evil.myshopify.com").split(".")[1]

    resp = await _post(app, f"{header}.{forged}.{signature}")

    assert resp.status_code == 401


async def test_issuer_and_destination_mismatch_is_rejected(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant())

    resp = await _post(app, _token(iss_shop="other.myshopify.com"))

    assert resp.status_code == 401


async def test_missing_and_malformed_authorization_are_rejected(ext_app):
    app, _, _ = ext_app

    assert (await _post(app, None)).status_code == 401
    assert (await _post(app, "not-a-jwt")).status_code == 401


async def test_unsigned_alg_none_token_is_rejected(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant())
    now = int(time.time())
    unsigned = jwt.encode(
        {"iss": "https://acme.myshopify.com/admin", "dest": "https://acme.myshopify.com",
         "aud": CLIENT_ID, "exp": now + 300, "nbf": now - 5},
        key=None,
        algorithm="none",
    )

    assert (await _post(app, unsigned)).status_code == 401


async def test_token_for_tenant_a_cannot_read_tenant_b_order(ext_app):
    app, make_tenant, seed = ext_app
    await make_tenant(slug="acme", shopify_shop_domain="acme.myshopify.com")
    tenant_b = await make_tenant(slug="beta", shopify_shop_domain="beta.myshopify.com")
    await seed(tenant_b, "2002", url="https://pay.example.com/pay/bancard_vpos/beta/t")

    resp = await _post(app, _token(shop="acme.myshopify.com"), order_id="2002")

    assert resp.status_code == 200
    assert resp.json() == NEUTRAL


async def test_paid_order_returns_neutral_response(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant(), status="paid")

    resp = await _post(app, _token())

    assert resp.status_code == 200
    assert resp.json() == NEUTRAL


async def test_non_vpos_intent_returns_neutral_response(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant(), provider="pagopar")

    assert (await _post(app, _token())).json() == NEUTRAL


async def test_tenant_not_on_vpos_returns_neutral_response(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant(payment_provider=PaymentProvider.PAGOPAR))

    assert (await _post(app, _token())).json() == NEUTRAL


async def test_order_without_intent_and_unknown_order_return_neutral(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant(), "1001")

    assert (await _post(app, _token(), order_id="gid://shopify/Order/9999")).json() == NEUTRAL
    assert (await _post(app, _token(), order_id="garbage")).json() == NEUTRAL


async def test_unknown_shop_returns_neutral_response(ext_app):
    app, make_tenant, _ = ext_app
    await make_tenant()

    resp = await _post(app, _token(shop="nobody.myshopify.com"))

    assert resp.status_code == 200
    assert resp.json() == NEUTRAL


async def test_preflight_allows_authorization_header_from_any_origin(ext_app):
    app, _, _ = ext_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.options(
            PATH,
            headers={
                "Origin": "https://extensions.shopifycdn.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()


async def test_requests_are_rate_limited(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant())
    statuses = [(await _post(app, _token())).status_code for _ in range(70)]

    assert statuses[0] == 200
    assert 429 in statuses


async def test_missing_client_secret_fails_closed(ext_app):
    app, make_tenant, seed = ext_app
    await seed(await make_tenant())
    app.state.settings = app.state.settings.model_copy(update={"shopify_api_secret": ""})

    resp = await _post(app, _token())

    assert resp.status_code == 503
    assert "pay_url" not in resp.text


def test_route_is_registered_in_the_application(monkeypatch):
    from bims_shopify import main
    from bims_shopify.config import Settings

    monkeypatch.setattr(main, "get_settings", lambda: Settings(admin_token="t", fernet_key="x"))
    app = main.create_app()

    assert PATH in app.openapi()["paths"]
