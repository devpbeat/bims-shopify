"""GET/POST /api/portal/{slug}/products-without-photos: auth, isolation, states, paging."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.models import OpsJobModel
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.api import portal, portal_products_without_photos, tenants
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ops.job_runner import JobConflictError

ADMIN = {"Authorization": "Bearer test-admin-token"}
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _tenant(slug: str) -> Tenant:
    return Tenant(
        id=None, slug=slug, bims_base_url="https://bims.example.com", bims_api_key="k",
        shopify_shop_domain=f"{slug}.myshopify.com", shopify_access_token="shpat_secret",
        shopify_webhook_secret="w", shopify_location_id="gid://shopify/Location/1",
        bims_posale_id=1, bims_warehouse_id=1, bims_company_id=1, bims_currency_id=1,
        bims_payment_method_id=1, default_customer_contact_id=1,
    )


class _StubRunner:
    def __init__(self) -> None:
        self.started: list[tuple[int, str, dict]] = []
        self.conflict = False

    async def start_job(self, tenant_id, command, options):
        if self.conflict:
            raise JobConflictError("already running")
        self.started.append((tenant_id, command, options))

        class _Job:
            id = 77
            status = "queued"

        return _Job()


@pytest.fixture
async def world():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="test-admin-token",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from alembic.config import Config

    from alembic import command

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = settings.database_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = factory
    app.state.job_runner = _StubRunner()
    app.include_router(tenants.router)
    app.include_router(portal.router)
    app.include_router(portal_products_without_photos.router)

    async with factory() as session:
        repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
        acme = await repo.create(_tenant("acme"))
        beta = await repo.create(_tenant("beta"))
        await session.commit()

    yield app, factory, acme, beta
    await engine.dispose()
    os.unlink(db_file.name)


def _row(n: int, *, status="ACTIVE", on_store=True, title=None, skus=None, stock=2.0) -> dict:
    return {
        "product_id": f"gid://shopify/Product/{n}",
        "title": title or f"Product {n}",
        "skus": [f"SKU-{n}"] if skus is None else skus,
        "status": status,
        "on_online_store": on_store,
        "stock": stock,
    }


def _result(rows: list[dict], **extra) -> dict:
    live = sum(1 for r in rows if r["status"] == "ACTIVE" and r["on_online_store"] is not False)
    return {
        "rows": rows,
        "scanned": 100,
        "without_photos": len(rows),
        "live_without_photos": live,
        "truncated": False,
        "stock_as_of": "2026-11-05T10:00:00+00:00",
        **extra,
    }


async def _job(factory, tenant_id, status, *, result=None, minutes_ago=0, error=None, **fields):
    created = NOW - timedelta(minutes=minutes_ago)
    async with factory() as session:
        session.add(
            OpsJobModel(
                tenant_id=tenant_id, command="missing_photos", options={}, status=status,
                result=result, error=error, created_at=created,
                finished_at=created if status in ("succeeded", "failed", "interrupted") else None,
                **fields,
            )
        )
        await session.commit()


async def _get(app, slug="acme", headers=ADMIN, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(
            f"/api/portal/{slug}/products-without-photos", headers=headers, params=params
        )


async def _post(app, slug="acme", headers=ADMIN):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(
            f"/api/portal/{slug}/products-without-photos/scan", headers=headers
        )


async def test_requires_portal_auth(world):
    app, *_ = world

    assert (await _get(app, headers={})).status_code == 401
    assert (await _post(app, headers={})).status_code == 401


async def test_a_portal_token_works_for_its_own_tenant_only(world):
    app, *_ = world
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        minted = await client.post("/tenants/acme/portal-token", headers=ADMIN)
    headers = {"Authorization": f"Bearer {minted.json()['portal_token']}"}

    assert (await _get(app, headers=headers)).status_code == 200
    assert (await _get(app, slug="beta", headers=headers)).status_code == 401


async def test_never_scanned_is_an_honest_state_not_an_empty_list(world):
    app, *_ = world

    body = (await _get(app)).json()

    assert body["scan"]["status"] == "never"
    assert body["scan"]["scanned_at"] is None
    assert body["counts"] is None
    assert body["items"] == [] and body["total"] == 0


async def test_ready_scan_lists_live_products_by_default_with_the_admin_link(world):
    app, factory, acme, _ = world
    rows = [_row(1), _row(2, status="DRAFT"), _row(3, on_store=False), _row(4)]
    await _job(factory, acme.id, "succeeded", result=_result(rows))

    body = (await _get(app)).json()

    assert body["scan"]["status"] == "ready"
    assert body["scan"]["scanned_at"] is not None
    assert body["scan"]["stock_as_of"] == "2026-11-05T10:00:00+00:00"
    assert body["scope"] == "live"
    assert [i["skus"] for i in body["items"]] == [["SKU-1"], ["SKU-4"]]
    assert body["items"][0]["admin_url"] == "https://acme.myshopify.com/admin/products/1"
    assert body["counts"] == {"live": 2, "all": 4, "scanned": 100}
    assert body["total"] == 2


async def test_scope_all_and_query_filter_the_list(world):
    app, factory, acme, _ = world
    rows = [_row(1, title="Blue Shirt"), _row(2, status="DRAFT", title="Red Hat")]
    await _job(factory, acme.id, "succeeded", result=_result(rows))

    everything = (await _get(app, scope="all")).json()
    searched = (await _get(app, scope="all", q="hat")).json()

    assert everything["total"] == 2
    assert [i["title"] for i in searched["items"]] == ["Red Hat"]


async def test_pagination_is_bounded(world):
    app, factory, acme, _ = world
    await _job(factory, acme.id, "succeeded", result=_result([_row(n) for n in range(1, 8)]))

    page = (await _get(app, page=2, page_size=3)).json()

    assert page["total"] == 7
    assert [i["title"] for i in page["items"]] == ["Product 4", "Product 5", "Product 6"]
    assert (await _get(app, page_size=101)).status_code == 422
    assert (await _get(app, page=0)).status_code == 422
    assert (await _get(app, scope="bogus")).status_code == 422


async def test_zero_products_without_photos_is_a_ready_success_state(world):
    app, factory, acme, _ = world
    await _job(factory, acme.id, "succeeded", result=_result([]))

    body = (await _get(app)).json()

    assert body["scan"]["status"] == "ready"
    assert body["counts"] == {"live": 0, "all": 0, "scanned": 100}
    assert body["items"] == []


async def test_a_running_rescan_still_serves_the_previous_result(world):
    app, factory, acme, _ = world
    await _job(factory, acme.id, "succeeded", result=_result([_row(1)]), minutes_ago=30)
    await _job(
        factory, acme.id, "running", minutes_ago=1, progress_done=40, progress_total=100,
        message="scanning Shopify products",
    )

    body = (await _get(app)).json()

    assert body["scan"]["status"] == "running"
    assert body["scan"]["progress_done"] == 40 and body["scan"]["progress_total"] == 100
    assert len(body["items"]) == 1


async def test_a_failed_scan_is_reported_without_leaking_its_error_text(world):
    app, factory, acme, _ = world
    await _job(factory, acme.id, "failed", minutes_ago=1, error="ShopifyGraphQLError: token shpat_x")

    response = await _get(app)

    assert response.json()["scan"]["status"] == "failed"
    assert "shpat" not in response.text and "ShopifyGraphQLError" not in response.text


async def test_an_interrupted_scan_counts_as_failed(world):
    app, factory, acme, _ = world
    await _job(factory, acme.id, "interrupted", minutes_ago=1)

    assert (await _get(app)).json()["scan"]["status"] == "failed"


async def test_one_tenant_never_sees_another_tenants_products(world):
    app, factory, _, beta = world
    await _job(factory, beta.id, "succeeded", result=_result([_row(9, title="Beta Secret")]))

    acme_body = await _get(app, slug="acme", scope="all")
    beta_body = await _get(app, slug="beta", scope="all")

    assert acme_body.json()["scan"]["status"] == "never"
    assert "Beta Secret" not in acme_body.text
    assert [i["title"] for i in beta_body.json()["items"]] == ["Beta Secret"]
    assert beta_body.json()["items"][0]["admin_url"].startswith("https://beta.myshopify.com/")


async def test_secrets_stored_next_to_the_rows_are_never_returned(world):
    app, factory, acme, _ = world
    leaky = _row(1)
    leaky["checkout_url"] = "https://pay.example/secret"
    await _job(factory, acme.id, "succeeded", result=_result([leaky], access_token="shpat_secret"))

    text = (await _get(app, scope="all")).text

    assert "checkout_url" not in text and "pay.example" not in text and "shpat" not in text


async def test_scan_starts_a_job_for_the_authenticated_tenant(world):
    app, _, acme, _ = world

    response = await _post(app)

    assert response.status_code == 202
    assert response.json() == {"job_id": 77, "status": "queued"}
    assert app.state.job_runner.started == [(acme.id, "missing_photos", {})]


async def test_scan_while_one_is_running_is_a_409(world):
    app, *_ = world
    app.state.job_runner.conflict = True

    assert (await _post(app)).status_code == 409
