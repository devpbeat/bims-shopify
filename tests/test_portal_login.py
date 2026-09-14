"""Tests for merchant-portal email+password login (session JWTs, rate limiting, admin upsert).

Reuses the same app-fixture shape as `test_portal_api.py` (temp sqlite DB
migrated to head via alembic) but adds the `tenants.router`'s
`portal-users` endpoint and exercises the JWT auth path on `portal.router`.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import RekeyReportModel
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import portal, tenants
from bims_shopify.api.portal_auth import SESSION_TOKEN_ALGORITHM, get_portal_session_secret
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

ADMIN_TOKEN = "test-admin-token"


async def _make_tenant(repo: SqlAlchemyTenantRepository, slug: str, shop_domain: str) -> Tenant:
    tenant = Tenant(
        id=None,
        slug=slug,
        bims_base_url="https://bims.example.com",
        bims_api_key="secretkey",
        shopify_shop_domain=shop_domain,
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
        active=True,
    )
    return await repo.create(tenant)


@pytest.fixture
async def app_and_tenants():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
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

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.include_router(tenants.router)
    app.include_router(portal.router)

    async with session_factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        acme = await _make_tenant(repo, "acme", "acme.myshopify.com")
        other = await _make_tenant(repo, "other", "other.myshopify.com")
        session.add(RekeyReportModel(tenant_id=acme.id, payload={}))
        await session.commit()

    yield app, acme, other
    await engine.dispose()
    os.unlink(db_file.name)


async def _create_portal_user(app: FastAPI, slug: str, email: str, password: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/tenants/{slug}/portal-users",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"email": email, "password": password, "name": "Rafa"},
        )
    assert response.status_code == 201, response.text


async def test_admin_creates_portal_user(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _create_portal_user(app, "acme", "rafa@acme.com", "correct-password")


async def test_login_success_returns_session_jwt(app_and_tenants):
    app, acme, _other = app_and_tenants
    await _create_portal_user(app, "acme", "rafa@acme.com", "correct-password")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/portal/acme/login",
            json={"email": "rafa@acme.com", "password": "correct-password"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "token" in body
    assert body["expires_in"] == 24 * 3600

    claims = jwt.decode(
        body["token"],
        get_portal_session_secret(app.state.settings),
        algorithms=[SESSION_TOKEN_ALGORITHM],
    )
    assert claims["tenant_id"] == acme.id


async def test_login_wrong_password_returns_401(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _create_portal_user(app, "acme", "rafa@acme.com", "correct-password")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/portal/acme/login",
            json={"email": "rafa@acme.com", "password": "wrong-password"},
        )
    assert response.status_code == 401


async def test_login_inactive_user_returns_401(app_and_tenants):
    app, acme, _other = app_and_tenants
    await _create_portal_user(app, "acme", "rafa@acme.com", "correct-password")

    session_factory = app.state.session_factory
    async with session_factory() as session:
        from bims_shopify.adapters.persistence.portal_user_repository import (
            SqlAlchemyPortalUserRepository,
        )

        repo = SqlAlchemyPortalUserRepository(session)
        user = await repo.get_by_tenant_and_email(acme.id, "rafa@acme.com")
        user.active = False
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/portal/acme/login",
            json={"email": "rafa@acme.com", "password": "correct-password"},
        )
    assert response.status_code == 401


async def test_login_wrong_tenant_slug_returns_401(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _create_portal_user(app, "acme", "rafa@acme.com", "correct-password")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/portal/other/login",
            json={"email": "rafa@acme.com", "password": "correct-password"},
        )
    assert response.status_code == 401


async def test_login_rate_limited_after_five_attempts(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _create_portal_user(app, "acme", "rafa@acme.com", "correct-password")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(5):
            response = await client.post(
                "/api/portal/acme/login",
                json={"email": "rafa@acme.com", "password": "wrong-password"},
            )
            assert response.status_code == 401
        sixth = await client.post(
            "/api/portal/acme/login",
            json={"email": "rafa@acme.com", "password": "wrong-password"},
        )
    assert sixth.status_code == 429


async def test_session_jwt_accepted_on_portal_endpoints(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _create_portal_user(app, "acme", "rafa@acme.com", "correct-password")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/portal/acme/login",
            json={"email": "rafa@acme.com", "password": "correct-password"},
        )
        token = login.json()["token"]

        response = await client.get(
            "/api/portal/acme/rekey-report", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200


async def test_expired_session_jwt_rejected(app_and_tenants):
    app, acme, _other = app_and_tenants
    secret = get_portal_session_secret(app.state.settings)
    expired_token = jwt.encode(
        {
            "tenant_id": acme.id,
            "user_id": 1,
            "iat": datetime.now(UTC) - timedelta(hours=48),
            "exp": datetime.now(UTC) - timedelta(hours=24),
        },
        secret,
        algorithm=SESSION_TOKEN_ALGORITHM,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/rekey-report",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
    assert response.status_code == 401


async def test_session_jwt_for_other_tenant_rejected(app_and_tenants):
    app, _acme, other = app_and_tenants
    secret = get_portal_session_secret(app.state.settings)
    token_for_other = jwt.encode(
        {
            "tenant_id": other.id,
            "user_id": 1,
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=24),
        },
        secret,
        algorithm=SESSION_TOKEN_ALGORITHM,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/rekey-report",
            headers={"Authorization": f"Bearer {token_for_other}"},
        )
    assert response.status_code == 401


async def test_admin_portal_user_upsert_requires_admin_auth(app_and_tenants):
    app, _acme, _other = app_and_tenants
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/tenants/acme/portal-users",
            json={"email": "rafa@acme.com", "password": "correct-password"},
        )
    assert response.status_code == 401


async def test_admin_portal_user_upsert_resets_password(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _create_portal_user(app, "acme", "rafa@acme.com", "first-password")
    await _create_portal_user(app, "acme", "rafa@acme.com", "second-password")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        stale = await client.post(
            "/api/portal/acme/login",
            json={"email": "rafa@acme.com", "password": "first-password"},
        )
        fresh = await client.post(
            "/api/portal/acme/login",
            json={"email": "rafa@acme.com", "password": "second-password"},
        )
    assert stale.status_code == 401
    assert fresh.status_code == 200
