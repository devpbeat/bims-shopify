"""Signed vPOS iframe page plus the return and cancel pages."""
from __future__ import annotations

from datetime import timedelta

import pytest
import respx
from httpx import ASGITransport, AsyncClient

from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.checkout_link import (
    derive_link_secret,
    sign_checkout_token,
    sign_navigation_token,
)
from bims_shopify.domain.payment import PaymentIntent

PRODUCTION = "https://vpos.infonet.com.py"
STAGING = "https://vpos.infonet.com.py:8888"
TTL = timedelta(hours=1)


def _token(settings, *, slug="acme", order="1001", pid="pid-abc", ttl=TTL):
    return sign_checkout_token(derive_link_secret(settings), slug, order, pid, ttl=ttl)


async def _get(app, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


def _csp_sources(response, directive: str) -> list[str]:
    for part in response.headers["content-security-policy"].split(";"):
        name, *sources = part.split()
        if name == directive:
            return sources
    return []


@respx.mock
async def test_staging_page_loads_staging_script_and_creates_form(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert f'src="{STAGING}/checkout/javascript/dist/bancard-checkout-3.0.0.js"' in resp.text
    assert 'id="iframe-container"' in resp.text
    assert 'Bancard.Checkout.createForm("iframe-container", "pid-abc"' in resp.text
    assert not respx.calls


async def test_production_page_uses_production_origin_only(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant(provider_config={"public_key": "k", "environment": "production"})

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert f'src="{PRODUCTION}/checkout/javascript/dist/bancard-checkout-3.0.0.js"' in resp.text
    for directive in ("script-src", "frame-src", "connect-src"):
        assert PRODUCTION in _csp_sources(resp, directive)
        assert STAGING not in _csp_sources(resp, directive)


async def test_staging_csp_excludes_bare_production_origin(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    for directive in ("script-src", "frame-src", "connect-src"):
        assert STAGING in _csp_sources(resp, directive)
        assert PRODUCTION not in _csp_sources(resp, directive)
    nonce = next(s for s in _csp_sources(resp, "script-src") if s.startswith("'nonce-"))
    assert f'nonce="{nonce[7:-1]}"' in resp.text


async def test_page_sends_hardening_headers(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


@respx.mock
async def test_query_amount_is_ignored(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}?amount=1")

    assert resp.status_code == 200
    assert not respx.calls


async def test_hostile_process_id_cannot_break_out_of_the_script(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    hostile = '";</script><script>alert(1)</script>'

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings, pid=hostile)}")

    assert "</script><script>" not in resp.text
    assert "<script>alert" not in resp.text


@respx.mock
async def test_expired_token_is_gone_with_no_outbound_calls(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings, ttl=timedelta(seconds=-5))}")

    assert resp.status_code == 410
    assert not respx.calls


@pytest.mark.parametrize("flavour", ["tampered", "wrong_slug", "no_process_id", "garbage"])
@respx.mock
async def test_invalid_tokens_are_forbidden_with_no_outbound_calls(vpos_app, flavour):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    await make_tenant(slug="other")
    token = {
        "tampered": lambda: _token(settings)[:-1] + ("A" if _token(settings)[-1] != "A" else "B"),
        "wrong_slug": lambda: _token(settings, slug="other"),
        "no_process_id": lambda: sign_navigation_token(
            derive_link_secret(settings), "acme", "1001", ttl=TTL
        ),
        "garbage": lambda: "not-a-token",
    }[flavour]()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{token}")

    assert resp.status_code == 403
    assert "pid-abc" not in resp.text
    assert not respx.calls


async def test_unknown_inactive_or_foreign_provider_tenant_is_forbidden(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant(slug="off", active=False)

    for slug in ("missing", "off"):
        resp = await _get(app, f"/pay/bancard_vpos/{slug}/{_token(settings, slug=slug)}")
        assert resp.status_code == 403


async def test_paid_order_shows_already_paid_page_without_the_form(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    async with app.state.session_factory() as session:
        await SqlAlchemyPaymentIntentRepository(session).save(
            tenant.id,
            "bancard_vpos",
            PaymentIntent(tenant_slug="acme", order_id="1001", amount=1.0, currency="PYG",
                          provider_reference="100100", status="paid"),
        )

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.status_code == 200
    assert "already been paid" in resp.text
    assert "bancard-checkout" not in resp.text


@pytest.mark.parametrize("outcome,text", [("return", "confirming"), ("cancel", "cancelled")])
async def test_return_and_cancel_pages_change_nothing(vpos_app, outcome, text):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    nav = sign_navigation_token(derive_link_secret(settings), "acme", "1001", ttl=TTL)
    async with app.state.session_factory() as session:
        await SqlAlchemyPaymentIntentRepository(session).save(
            tenant.id,
            "bancard_vpos",
            PaymentIntent(tenant_slug="acme", order_id="1001", amount=1.0, currency="PYG",
                          provider_reference="100100", status="pending"),
        )

    with respx.mock:
        resp = await _get(app, f"/pay/bancard_vpos/acme/{outcome}/{nav}")
        assert not respx.calls

    assert resp.status_code == 200
    assert text in resp.text
    async with app.state.session_factory() as session:
        stored = await SqlAlchemyPaymentIntentRepository(session).get_by_order_id(
            tenant.id, "bancard_vpos", "1001"
        )
    assert stored.status == "pending"


async def test_return_page_rejects_a_forged_token(vpos_app):
    app, _, make_tenant = vpos_app
    await make_tenant()

    resp = await _get(app, "/pay/bancard_vpos/acme/return/forged.token")

    assert resp.status_code == 403
