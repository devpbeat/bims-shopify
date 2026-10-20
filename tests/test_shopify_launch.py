"""Tests for the Shopify admin app-launch SSO handler (``GET /``)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import AuditLogModel
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import portal, shopify_launch
from bims_shopify.api.portal_auth import decode_session_token, get_portal_session_secret
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

API_SECRET = "test-client-secret"
SHOP = "1ncq0u-03.myshopify.com"
HOST = "YWRtaW4uc2hvcGlmeS5jb20vc3RvcmUvMW5jcTB1LTAz"
SESSION_PARAM = "2c402abd-session-value-must-never-be-logged"


def _sign(params: dict[str, str], secret: str = API_SECRET) -> str:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac")
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _launch_params(
    *, shop: str = SHOP, age: int = 0, secret: str = API_SECRET
) -> dict[str, str]:
    params = {
        "host": HOST,
        "session": SESSION_PARAM,
        "shop": shop,
        "timestamp": str(int(time.time()) - age),
    }
    params["hmac"] = _sign(params, secret)
    return params


async def _make_tenant(repo, *, slug: str, domain: str, active: bool = True) -> Tenant:
    return await repo.create(
        Tenant(
            id=None,
            slug=slug,
            bims_base_url="https://bims.example.com",
            bims_api_key="secretkey",
            shopify_shop_domain=domain,
            shopify_access_token="shpat_test",
            shopify_webhook_secret="whsecret",
            shopify_location_id="gid://shopify/Location/1",
            bims_posale_id=1,
            bims_warehouse_id=1,
            bims_company_id=1,
            bims_currency_id=1,
            bims_payment_method_id=1,
            default_customer_contact_id=1,
            reorder_threshold=0.0,
            reorder_strategy=ReorderStrategy.NONE,
            payment_provider=PaymentProvider.BANCARD,
            provider_config={},
            field_mappings={},
            active=active,
        )
    )


@pytest.fixture
async def ctx():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="test-admin-token",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
        shopify_api_key="test-client-id",
        shopify_api_secret=API_SECRET,
    )
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    from alembic.config import Config

    from alembic import command

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = settings.database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        # Slug deliberately differs from the domain subdomain (production shape).
        active = await _make_tenant(repo, slug="mystore", domain=SHOP)
        await _make_tenant(
            repo, slug="dormant", domain="dormant-shop.myshopify.com", active=False
        )

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.include_router(shopify_launch.router)
    app.include_router(portal.router)

    yield app, settings, session_factory, active

    await engine.dispose()
    os.unlink(db_file.name)


async def _launch(app, params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/", params=params, follow_redirects=False)


def _assert_neutral_rejection(resp) -> None:
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"
    assert "set-cookie" not in resp.headers
    assert "token" not in resp.text.lower()


async def _audit_entries(session_factory) -> list[AuditLogModel]:
    async with session_factory() as session:
        return list((await session.execute(select(AuditLogModel))).scalars())


def _extract_token(html: str) -> str:
    match = re.search(r"const token = (\".*?\");", html)
    assert match, "interstitial page must embed the session token"
    return json.loads(match.group(1))


async def test_valid_launch_creates_session_and_lands_in_portal(ctx):
    app, settings, _sf, tenant = ctx
    resp = await _launch(app, _launch_params())

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "'nonce-" in csp
    assert "/portal/mystore" in resp.text
    assert "bims-portal-token:mystore" in resp.text

    claims = decode_session_token(
        secret=get_portal_session_secret(settings), token=_extract_token(resp.text)
    )
    assert claims["tenant_id"] == tenant.id
    assert claims["exp"] - claims["iat"] == 24 * 3600


async def test_issued_session_is_accepted_by_portal_endpoint(ctx):
    app, _settings, _sf, _tenant = ctx
    resp = await _launch(app, _launch_params())
    token = _extract_token(resp.text)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(
            "/api/portal/mystore/payments", headers={"Authorization": f"Bearer {token}"}
        )
        anon = await client.get("/api/portal/mystore/payments")

    assert ok.status_code == 200
    assert anon.status_code == 401


async def test_tampered_hmac_is_rejected(ctx):
    app, *_ = ctx
    params = _launch_params()
    params["hmac"] = "0" * 64
    _assert_neutral_rejection(await _launch(app, params))


async def test_wrong_secret_is_rejected(ctx):
    app, *_ = ctx
    _assert_neutral_rejection(await _launch(app, _launch_params(secret="other-secret")))


async def test_tampered_param_is_rejected(ctx):
    app, *_ = ctx
    params = _launch_params()
    params["host"] = "ZXZpbA"
    _assert_neutral_rejection(await _launch(app, params))


async def test_missing_hmac_is_rejected(ctx):
    app, *_ = ctx
    params = _launch_params()
    del params["hmac"]
    _assert_neutral_rejection(await _launch(app, params))


async def test_stale_timestamp_is_rejected(ctx):
    app, *_ = ctx
    _assert_neutral_rejection(await _launch(app, _launch_params(age=600)))


async def test_future_timestamp_is_rejected(ctx):
    app, *_ = ctx
    _assert_neutral_rejection(await _launch(app, _launch_params(age=-600)))


async def test_non_numeric_timestamp_is_rejected(ctx):
    app, *_ = ctx
    params = _launch_params()
    params["timestamp"] = "yesterday"
    params["hmac"] = _sign(params)
    _assert_neutral_rejection(await _launch(app, params))


@pytest.mark.parametrize(
    "bad_shop",
    [
        "evil.example.com",
        "mystore.myshopify.com.evil.com",
        "evil.com/.myshopify.com",
        "-.myshopify.com",
        "a b.myshopify.com",
        ".myshopify.com",
        "",
    ],
)
async def test_malformed_shop_is_rejected_even_if_correctly_signed(ctx, bad_shop):
    app, *_ = ctx
    _assert_neutral_rejection(await _launch(app, _launch_params(shop=bad_shop)))


async def test_duplicate_query_keys_are_rejected(ctx):
    app, *_ = ctx
    pairs = [*_launch_params().items(), ("shop", "other.myshopify.com")]
    _assert_neutral_rejection(await _launch(app, pairs))


async def test_unknown_and_inactive_shops_are_indistinguishable(ctx):
    app, _settings, sf, _tenant = ctx
    unknown = await _launch(app, _launch_params(shop="nobody-here.myshopify.com"))
    inactive = await _launch(app, _launch_params(shop="dormant-shop.myshopify.com"))

    _assert_neutral_rejection(unknown)
    _assert_neutral_rejection(inactive)
    assert unknown.text == inactive.text
    assert unknown.headers.keys() == inactive.headers.keys()

    assert {e.action for e in await _audit_entries(sf)} == {"portal.sso_failed"}


async def test_slug_differing_from_subdomain_is_resolved_by_stored_domain(ctx):
    """Regression: tenant slug ``mystore`` != domain subdomain ``1ncq0u-03``."""
    app, _settings, _sf, _tenant = ctx
    assert SHOP.split(".")[0] != "mystore"
    resp = await _launch(app, _launch_params())
    assert resp.status_code == 200
    assert "/portal/mystore" in resp.text


async def test_shop_case_and_whitespace_are_normalized(ctx):
    app, *_ = ctx
    resp = await _launch(app, _launch_params(shop=f"  {SHOP.upper()} "))
    assert resp.status_code == 200
    assert "/portal/mystore" in resp.text


async def test_success_and_failure_are_audited_without_secrets(ctx, capsys):
    app, _settings, sf, tenant = ctx
    params = _launch_params()
    await _launch(app, params)
    await _launch(app, _launch_params(shop="dormant-shop.myshopify.com"))

    entries = await _audit_entries(sf)
    by_action = {e.action: e for e in entries}
    assert by_action["portal.sso_login"].tenant_id == tenant.id
    assert by_action["portal.sso_login"].entity == "portal_sso"
    assert by_action["portal.sso_failed"].tenant_id is not None
    assert by_action["portal.sso_failed"].payload == {"reason": "inactive_tenant"}

    dump = json.dumps([e.payload for e in entries], default=str) + "".join(
        f"{e.entity_id}{e.action}" for e in entries
    )
    captured = capsys.readouterr()
    for secret in (params["hmac"], SESSION_PARAM, API_SECRET):
        assert secret not in dump
        assert secret not in captured.out + captured.err


async def test_empty_configured_secret_never_validates(ctx):
    app, settings, *_ = ctx
    settings.shopify_api_secret = ""
    _assert_neutral_rejection(await _launch(app, _launch_params(secret="")))


async def test_bare_root_redirects_to_portal_login(ctx):
    app, *_ = ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/", follow_redirects=False)
    _assert_neutral_rejection(resp)
