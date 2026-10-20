"""Signed vPOS iframe page plus the return and cancel pages."""
from __future__ import annotations

from datetime import timedelta

import pytest
import respx
from httpx import ASGITransport, AsyncClient

from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
import httpx
import json

from bims_shopify.checkout_link import (
    derive_link_secret,
    sign_checkout_token,
    sign_navigation_token,
)
from bims_shopify.domain.payment import PaymentIntent
from bims_shopify.domain.tenant import PaymentProvider

PRODUCTION = "https://vpos.infonet.com.py"
STAGING = "https://vpos.infonet.com.py:8888"
TTL = timedelta(hours=1)


SHOPIFY = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
SINGLE_BUY = f"{STAGING}/vpos/api/0.3/single_buy"
ROLLBACK = f"{STAGING}/vpos/api/0.3/single_buy/rollback"
CONFIRMATIONS = f"{STAGING}/vpos/api/0.3/single_buy/confirmations"
STATUS_URL = "https://acme.myshopify.com/1/orders/abc/authenticate?key=k"


def _token(settings, *, slug="acme", order="1001", ttl=TTL):
    return sign_navigation_token(derive_link_secret(settings), slug, order, ttl=ttl)


def _order(amount="130.00", status="PENDING", currency="PYG") -> httpx.Response:
    order = {
        "id": "gid://shopify/Order/1001",
        "displayFinancialStatus": status,
        "totalPriceSet": {"shopMoney": {"amount": amount, "currencyCode": currency}},
    }
    return httpx.Response(200, json={"data": {"order": order}})


def _buy(*process_ids: str):
    return respx.post(SINGLE_BUY).mock(
        side_effect=[
            httpx.Response(200, json={"status": "success", "process_id": pid}) for pid in process_ids
        ]
    )


def _mock(amount="130.00", *process_ids):
    shopify = respx.post(SHOPIFY).mock(return_value=_order(amount))
    return shopify, _buy(*(process_ids or ("pid-abc",)))


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
    _mock()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert f'src="{STAGING}/checkout/javascript/dist/bancard-checkout-3.0.0.js"' in resp.text
    assert 'id="iframe-container"' in resp.text
    assert 'Bancard.Checkout.createForm("iframe-container", "pid-abc"' in resp.text


@respx.mock
async def test_production_page_uses_production_origin_only(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant(provider_config={"public_key": "k", "environment": "production"})
    respx.post(SHOPIFY).mock(return_value=_order())
    respx.post(f"{PRODUCTION}/vpos/api/0.3/single_buy").mock(
        return_value=httpx.Response(200, json={"status": "success", "process_id": "pid-prod"})
    )

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert f'src="{PRODUCTION}/checkout/javascript/dist/bancard-checkout-3.0.0.js"' in resp.text
    for directive in ("script-src", "frame-src", "connect-src"):
        assert PRODUCTION in _csp_sources(resp, directive)
        assert STAGING not in _csp_sources(resp, directive)


@respx.mock
async def test_staging_csp_excludes_bare_production_origin(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    _mock()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    for directive in ("script-src", "frame-src", "connect-src"):
        assert STAGING in _csp_sources(resp, directive)
        assert PRODUCTION not in _csp_sources(resp, directive)
    nonce = next(s for s in _csp_sources(resp, "script-src") if s.startswith("'nonce-"))
    assert f'nonce="{nonce[7:-1]}"' in resp.text


@respx.mock
async def test_page_sends_hardening_headers(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    _mock()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


@respx.mock
async def test_query_amount_is_ignored_and_the_live_total_is_charged(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    _, buy = _mock("150000.50")

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}?amount=1")

    assert resp.status_code == 200
    assert json.loads(buy.calls[0].request.content)["operation"]["amount"] == "150000.50"


@respx.mock
async def test_hostile_process_id_cannot_break_out_of_the_script(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    _mock("130.00", '";</script><script>alert(1)</script>')

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert "</script><script>" not in resp.text
    assert "<script>alert" not in resp.text


@respx.mock
async def test_refreshing_the_page_reuses_one_bancard_operation(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    shopify, buy = _mock()

    pages = [await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}") for _ in range(5)]

    assert buy.call_count == 1
    assert shopify.call_count == 5
    assert all('"pid-abc"' in page.text for page in pages)


@respx.mock
async def test_edited_order_total_rolls_back_and_mints_with_the_new_amount(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    respx.post(SHOPIFY).mock(side_effect=[_order("130.00"), _order("175000.00")])
    buy = _buy("pid-old", "pid-new")
    rollback = respx.post(ROLLBACK).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": "PaymentNotFoundError", "level": "error"}]}
        )
    )

    first = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")
    second = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert '"pid-old"' in first.text and '"pid-new"' in second.text
    assert rollback.call_count == 1
    assert json.loads(rollback.calls[0].request.content)["operation"]["shop_process_id"] == "100"
    new_body = json.loads(buy.calls[1].request.content)["operation"]
    assert new_body["amount"] == "175000.00" and new_body["shop_process_id"] == 101


@respx.mock
async def test_legacy_signed_process_id_link_still_opens_the_dynamic_page(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    _mock()
    legacy = sign_checkout_token(derive_link_secret(settings), "acme", "1001", "stale", ttl=TTL)

    resp = await _get(app, f"/pay/bancard_vpos/acme/{legacy}")

    assert resp.status_code == 200 and '"pid-abc"' in resp.text


@respx.mock
async def test_order_paid_in_shopify_shows_already_paid_without_minting(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    respx.post(SHOPIFY).mock(return_value=_order(status="PAID"))
    buy = _buy("pid-abc")

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.status_code == 200 and "already been paid" in resp.text
    assert buy.call_count == 0


@respx.mock
async def test_unknown_order_and_non_pyg_order_are_clean_errors(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    buy = _buy("pid-abc")
    respx.post(SHOPIFY).mock(side_effect=[
        httpx.Response(200, json={"data": {"order": None}}), _order(currency="USD"),
    ])

    missing = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")
    foreign = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert (missing.status_code, foreign.status_code) == (404, 422)
    assert buy.call_count == 0


@respx.mock
async def test_shopify_outage_renders_an_error_page_not_a_stack_trace(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    respx.post(SHOPIFY).mock(side_effect=httpx.ConnectError("down"))

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.status_code == 503
    assert "try again" in resp.text and "Traceback" not in resp.text


@respx.mock
async def test_bancard_unreachable_renders_an_error_page_and_audits(vpos_app):
    from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger  # noqa: F401

    app, settings, make_tenant = vpos_app
    await make_tenant()
    respx.post(SHOPIFY).mock(return_value=_order())
    respx.post(SINGLE_BUY).mock(side_effect=httpx.ConnectError("down"))

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.status_code == 502
    assert "payment service" in resp.text and "Traceback" not in resp.text
    assert await _audit_actions(app) == ["payment.page_error"]


@respx.mock
async def test_credential_rejection_renders_an_error_page_and_audits_without_secrets(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    respx.post(SHOPIFY).mock(return_value=_order())
    respx.post(SINGLE_BUY).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": "InvalidTokenError", "level": "error"}]}
        )
    )

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert resp.status_code == 503
    assert "InvalidTokenError" not in resp.text and "vpos-private-key" not in resp.text
    assert await _audit_actions(app) == ["payment.page_error"]


async def _audit_actions(app) -> list[str]:
    import sqlalchemy as sa

    from bims_shopify.adapters.persistence.models import AuditLogModel

    async with app.state.session_factory() as session:
        rows = (await session.execute(sa.select(AuditLogModel))).scalars().all()
    return [row.action for row in rows]


@respx.mock
async def test_expired_token_is_gone_with_no_outbound_calls(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings, ttl=timedelta(seconds=-5))}")

    assert resp.status_code == 410
    assert not respx.calls


@pytest.mark.parametrize("flavour", ["tampered", "wrong_slug", "garbage"])
@respx.mock
async def test_invalid_tokens_are_forbidden_with_no_outbound_calls(vpos_app, flavour):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    await make_tenant(slug="other")
    token = {
        "tampered": lambda: _token(settings)[:-1] + ("A" if _token(settings)[-1] != "A" else "B"),
        "wrong_slug": lambda: _token(settings, slug="other"),
        "garbage": lambda: "not-a-token",
    }[flavour]()

    resp = await _get(app, f"/pay/bancard_vpos/acme/{token}")

    assert resp.status_code == 403
    assert not respx.calls


@respx.mock
async def test_unknown_inactive_or_foreign_provider_tenant_is_forbidden(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant(slug="off", active=False)
    await make_tenant(slug="pago", payment_provider=PaymentProvider.PAGOPAR, provider_config={})

    for slug in ("missing", "off", "pago"):
        resp = await _get(app, f"/pay/bancard_vpos/{slug}/{_token(settings, slug=slug)}")
        assert resp.status_code == 403
    assert not respx.calls


@respx.mock
async def test_locally_paid_intent_shows_already_paid_without_any_call(vpos_app):
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
    assert not respx.calls


@pytest.mark.parametrize("outcome,text", [("cancel", "cancelled")])
async def test_cancel_page_changes_nothing(vpos_app, outcome, text):
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


async def _seed_attempt(app, tenant, *, status="pending"):
    async with app.state.session_factory() as session:
        await SqlAlchemyPaymentIntentRepository(session).save(
            tenant.id,
            "bancard_vpos",
            PaymentIntent(tenant_slug="acme", order_id="1001", amount=130.0, currency="PYG",
                          provider_reference="100100", process_id="pid-abc", status=status,
                          checkout_url="https://pay.example.com/x"),
        )


def _confirmation(code="00", amount="130.00") -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "success",
              "confirmation": {"response_code": code, "amount": amount, "currency": "PYG"}},
    )


def _order_with_status(url=STATUS_URL) -> httpx.Response:
    order = {
        "id": "gid://shopify/Order/1001",
        "displayFinancialStatus": "PENDING",
        "statusPageUrl": url,
        "totalPriceSet": {"shopMoney": {"amount": "130.00", "currencyCode": "PYG"}},
    }
    return httpx.Response(200, json={"data": {"order": order}})


async def _return(app, settings, query=""):
    nav = _token(settings)
    return await _get(app, f"/pay/bancard_vpos/acme/return/{nav}{query}")


async def _stored_status(app, tenant) -> str:
    async with app.state.session_factory() as session:
        stored = await SqlAlchemyPaymentIntentRepository(session).get_by_order_id(
            tenant.id, "bancard_vpos", "1001"
        )
    return stored.status


@respx.mock
async def test_return_verifies_with_bancard_then_redirects_to_the_order_status_page(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant)
    confirmations = respx.post(CONFIRMATIONS).mock(return_value=_confirmation())
    shopify = respx.post(SHOPIFY).mock(return_value=_order_with_status())

    resp = await _return(app, settings)

    assert (resp.status_code, resp.headers["location"]) == (303, STATUS_URL)
    assert confirmations.call_count == 1 and shopify.call_count == 1
    assert "statusPageUrl" in json.loads(shopify.calls[0].request.content)["query"]
    assert await _stored_status(app, tenant) == "pending"


@respx.mock
async def test_return_without_a_status_url_shows_an_in_app_confirmation(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant)
    respx.post(CONFIRMATIONS).mock(return_value=_confirmation())
    respx.post(SHOPIFY).mock(return_value=_order_with_status(url=None))

    resp = await _return(app, settings)

    assert resp.status_code == 200 and "Payment received" in resp.text


@respx.mock
async def test_return_never_redirects_to_a_non_https_status_url(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant)
    respx.post(CONFIRMATIONS).mock(return_value=_confirmation())
    respx.post(SHOPIFY).mock(return_value=_order_with_status(url="javascript:alert(1)"))

    resp = await _return(app, settings)

    assert resp.status_code == 200 and "location" not in resp.headers


@respx.mock
async def test_return_trusts_bancard_not_the_browser(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant)
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": "SomethingElse", "level": "error"}]}
        )
    )

    resp = await _return(app, settings, "?status=success&response_code=00")

    assert resp.status_code == 200 and "still confirming" in resp.text
    assert "location" not in resp.headers


@respx.mock
async def test_return_while_bancard_is_unreachable_says_still_confirming(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant)
    respx.post(CONFIRMATIONS).mock(side_effect=httpx.ConnectError("down"))

    resp = await _return(app, settings)

    assert resp.status_code == 200 and "still confirming" in resp.text
    assert await _stored_status(app, tenant) == "pending"


@respx.mock
async def test_return_with_rejected_credentials_says_still_confirming_and_audits(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant)
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": "InvalidTokenError", "level": "error"}]}
        )
    )

    resp = await _return(app, settings)

    assert resp.status_code == 200 and "still confirming" in resp.text
    assert "InvalidTokenError" not in resp.text
    assert await _stored_status(app, tenant) == "pending"
    assert await _audit_actions(app) == ["payment.page_error"]


@pytest.mark.parametrize("key", ["PaymentNotFoundError", "BuyNotFoundError"])
@respx.mock
async def test_return_before_the_buyer_has_paid_says_still_confirming_not_declined(vpos_app, key):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant)
    respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": key, "level": "error"}]}
        )
    )

    resp = await _return(app, settings)

    assert resp.status_code == 200 and "still confirming" in resp.text
    assert "declined" not in resp.text and "location" not in resp.headers
    assert await _stored_status(app, tenant) == "pending"


@respx.mock
async def test_reopening_an_unpaid_attempt_with_an_unchanged_total_does_not_mint_again(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    _, buy = _mock()
    first = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")
    confirmations = respx.post(CONFIRMATIONS).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "messages": [{"key": "PaymentNotFoundError", "level": "error"}]}
        )
    )
    rollback = respx.post(ROLLBACK).mock(return_value=httpx.Response(200, json={"status": "success"}))

    returned = await _return(app, settings)
    reopened = await _get(app, f"/pay/bancard_vpos/acme/{_token(settings)}")

    assert "still confirming" in returned.text
    assert confirmations.call_count == 1
    assert '"pid-abc"' in first.text and '"pid-abc"' in reopened.text
    assert buy.call_count == 1 and rollback.call_count == 0
    assert await _stored_status(app, tenant) == "pending"


@respx.mock
async def test_declined_return_offers_a_retry_and_the_same_link_mints_a_new_attempt(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant)
    respx.post(CONFIRMATIONS).mock(return_value=_confirmation("05"))
    respx.post(ROLLBACK).mock(return_value=httpx.Response(200, json={"status": "success"}))
    respx.post(SHOPIFY).mock(return_value=_order())
    buy = _buy("pid-retry")

    declined = await _return(app, settings)

    nav = _token(settings)
    assert declined.status_code == 200 and "declined" in declined.text
    assert f'href="/pay/bancard_vpos/acme/{nav}"' in declined.text
    assert await _stored_status(app, tenant) == "failed"

    retry = await _get(app, f"/pay/bancard_vpos/acme/{nav}")

    assert '"pid-retry"' in retry.text
    assert json.loads(buy.calls[0].request.content)["operation"]["shop_process_id"] == 101


@respx.mock
async def test_return_for_an_already_paid_intent_redirects_without_asking_bancard(vpos_app):
    app, settings, make_tenant = vpos_app
    tenant = await make_tenant()
    await _seed_attempt(app, tenant, status="paid")
    shopify = respx.post(SHOPIFY).mock(return_value=_order_with_status())

    resp = await _return(app, settings)

    assert resp.status_code == 303 and shopify.call_count == 1


@respx.mock
async def test_return_before_any_attempt_exists_says_still_confirming(vpos_app):
    app, settings, make_tenant = vpos_app
    await make_tenant()

    resp = await _return(app, settings)

    assert resp.status_code == 200 and "still confirming" in resp.text
    assert not respx.calls


@pytest.mark.parametrize("flavour,status", [("expired", 410), ("tampered", 403)])
@respx.mock
async def test_return_rejects_bad_tokens_without_any_outbound_call(vpos_app, flavour, status):
    app, settings, make_tenant = vpos_app
    await make_tenant()
    nav = _token(settings, ttl=timedelta(seconds=-5)) if flavour == "expired" else _token(settings)[:-2] + "xx"

    resp = await _get(app, f"/pay/bancard_vpos/acme/return/{nav}")

    assert resp.status_code == status and not respx.calls
