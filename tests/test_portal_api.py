"""Integration tests for the merchant portal API and the admin portal-token mint endpoint.

Covers: token mint + hash storage, portal auth (missing/wrong token, wrong
tenant's token), report readback, each resolution action (including the
underlying Shopify mutation call), failed-mutation error recording,
idempotent re-posting, and 422 on an unknown variant_id.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

import httpx
import pytest
import respx
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
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

ADMIN_TOKEN = "test-admin-token"

REPORT_PAYLOAD = {
    "total_variants": 4,
    "empty_sku": 0,
    "already_keyed": 0,
    "planned_rewrites": [
        {
            "variant_id": "gid://shopify/ProductVariant/1",
            "product_id": "gid://shopify/Product/1",
            "product_title": "Widget",
            "old_sku": "OLD-1",
            "new_sku": "NEW-1",
            "bims_name": "Widget",
        }
    ],
    "name_mismatch": [
        {
            "variant_id": "gid://shopify/ProductVariant/2",
            "product_id": "gid://shopify/Product/2",
            "product_title": "Gadget",
            "old_sku": "OLD-2",
            "new_sku": "NEW-2",
            "bims_name": "Totally Different Name",
        }
    ],
    "unresolved": [
        {
            "variant_id": "gid://shopify/ProductVariant/3",
            "product_title": "Mystery Item",
            "sku": "UNKNOWN-3",
        }
    ],
    "duplicate_target": [
        {
            "variant_id": "gid://shopify/ProductVariant/4",
            "product_id": "gid://shopify/Product/4",
            "product_title": "Clashing Item",
            "old_sku": "OLD-4",
            "new_sku": "DUPLICATE",
            "bims_name": "Clashing Item",
        }
    ],
}


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

        session.add(RekeyReportModel(tenant_id=acme.id, payload=REPORT_PAYLOAD))
        await session.commit()

    yield app, acme, other
    await engine.dispose()
    os.unlink(db_file.name)


async def _mint_portal_token(app: FastAPI, slug: str) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/tenants/{slug}/portal-token",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    assert response.status_code == 200
    return response.json()["portal_token"]


async def test_mint_portal_token_stores_hash_not_plaintext(app_and_tenants):
    app, acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    session_factory = app.state.session_factory
    async with session_factory() as session:
        from bims_shopify.adapters.persistence.tenant_repository import (
            SqlAlchemyTenantRepository as Repo,
        )

        repo = Repo(session, SecretBox(app.state.settings.fernet_key))
        stored_hash = await repo.get_portal_token_hash(acme.id)

    assert stored_hash is not None
    assert stored_hash != token
    assert stored_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()


async def test_mint_portal_token_requires_admin_auth(app_and_tenants):
    app, _acme, _other = app_and_tenants
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/tenants/acme/portal-token")
    assert response.status_code == 401


async def test_portal_rejects_missing_token(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _mint_portal_token(app, "acme")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/portal/acme/rekey-report")
    assert response.status_code == 401


async def test_portal_rejects_wrong_token(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _mint_portal_token(app, "acme")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/rekey-report", headers={"Authorization": "Bearer wrong-token"}
        )
    assert response.status_code == 401


async def test_portal_rejects_other_tenants_token(app_and_tenants):
    app, _acme, _other = app_and_tenants
    await _mint_portal_token(app, "acme")
    other_token = await _mint_portal_token(app, "other")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/rekey-report",
            headers={"Authorization": f"Bearer {other_token}"},
        )
    assert response.status_code == 401


async def test_get_rekey_report_returns_latest_payload_and_resolutions(app_and_tenants):
    app, _acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/rekey-report", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["payload"] == REPORT_PAYLOAD
    assert body["resolutions"] == []
    assert body["created_at"] is not None


async def test_post_resolution_unknown_variant_returns_422(app_and_tenants):
    app, _acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/portal/acme/rekey-resolutions",
            headers={"Authorization": f"Bearer {token}"},
            json={"resolutions": [{"variant_id": "gid://shopify/ProductVariant/999", "action": "keep"}]},
        )
    assert response.status_code == 422


async def test_keep_and_ignore_are_recorded_without_shopify_call(app_and_tenants):
    app, _acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    with respx.mock(assert_all_called=False) as mock:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/portal/acme/rekey-resolutions",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "resolutions": [
                        {"variant_id": "gid://shopify/ProductVariant/3", "action": "keep"},
                        {"variant_id": "gid://shopify/ProductVariant/3", "action": "ignore"},
                    ]
                },
            )
        assert mock.calls.call_count == 0

    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["status"] == "recorded"
    # Second post for the same variant re-uses the settled resolution.
    assert results[1]["status"] == "already_resolved"


async def test_approve_sku_calls_bulk_update_variants(app_and_tenants):
    app, acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    graphql_url = f"https://{acme.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(graphql_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "productVariantsBulkUpdate": {
                            "productVariants": [{"id": "gid://shopify/ProductVariant/1", "sku": "NEW-1"}],
                            "userErrors": [],
                        }
                    }
                },
            )
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/portal/acme/rekey-resolutions",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "resolutions": [
                        {"variant_id": "gid://shopify/ProductVariant/1", "action": "approve_sku"}
                    ]
                },
            )
        assert route.call_count == 1
        sent_body = route.calls[0].request.content
        assert b"ProductVariantsBulkDelete" not in sent_body
        assert b"ProductVariantsBulkUpdate" in sent_body

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "applied"
    assert result["error"] is None


async def test_delete_calls_bulk_delete_variants(app_and_tenants):
    app, acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    graphql_url = f"https://{acme.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(graphql_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "productVariantsBulkDelete": {
                            "product": {"id": "gid://shopify/Product/1"},
                            "userErrors": [],
                        }
                    }
                },
            )
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/portal/acme/rekey-resolutions",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "resolutions": [
                        {"variant_id": "gid://shopify/ProductVariant/1", "action": "delete"}
                    ]
                },
            )
        assert route.call_count == 1
        sent_body = route.calls[0].request.content
        assert b"ProductVariantsBulkDelete" in sent_body

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "applied"


async def test_failed_mutation_records_status_failed_with_error(app_and_tenants):
    app, acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    graphql_url = f"https://{acme.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    with respx.mock(assert_all_called=True) as mock:
        mock.post(graphql_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "productVariantsBulkDelete": {
                            "product": None,
                            "userErrors": [{"field": ["id"], "message": "Variant not found"}],
                        }
                    }
                },
            )
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/portal/acme/rekey-resolutions",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "resolutions": [
                        {"variant_id": "gid://shopify/ProductVariant/1", "action": "delete"}
                    ]
                },
            )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "failed"
    assert "Variant not found" in result["error"]


async def test_idempotent_repost_returns_already_resolved_without_reapplying(app_and_tenants):
    app, acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    graphql_url = f"https://{acme.shopify_shop_domain}/admin/api/2025-07/graphql.json/"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(graphql_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "productVariantsBulkDelete": {
                            "product": {"id": "gid://shopify/Product/1"},
                            "userErrors": [],
                        }
                    }
                },
            )
        )
        transport = ASGITransport(app=app)
        payload = {
            "resolutions": [{"variant_id": "gid://shopify/ProductVariant/1", "action": "delete"}]
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/api/portal/acme/rekey-resolutions",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            second = await client.post(
                "/api/portal/acme/rekey-resolutions",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        assert route.call_count == 1

    assert first.json()["results"][0]["status"] == "applied"
    assert second.json()["results"][0]["status"] == "already_resolved"


async def test_portal_sync_status_returns_status_summary(app_and_tenants):
    app, _acme, _other = app_and_tenants
    token = await _mint_portal_token(app, "acme")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/portal/acme/sync-status", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert "last_run_at" in body
    assert "last_run_summary" in body
