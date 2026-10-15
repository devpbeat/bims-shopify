"""do_pull: the PULL worker end to end (BIMS over HTTP, our DB, Shopify read-only)."""
from __future__ import annotations

import httpx
import pytest
import respx

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.ops.sync_workers import do_pull, record_skip

pytestmark = pytest.mark.usefixtures("sequential_paging")

GRAPHQL = "https://acme.myshopify.com/admin/api/2025-07/graphql.json/"
BIMS_INDEX = "https://bims.example.com/api/products/index.json"
BIMS_STOCK = "https://bims.example.com/api/products_stocks/stock_fenicio.json"


def _variants(*skus: str) -> dict:
    return {
        "data": {
            "productVariants": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": f"gid://shopify/ProductVariant/{i}",
                        "sku": sku,
                        "product": {"id": f"gid://shopify/Product/{i}", "title": sku},
                        "inventoryItem": {"id": f"gid://shopify/InventoryItem/{i}", "tracked": True},
                    }
                    for i, sku in enumerate(skus, start=1)
                ],
            }
        }
    }


def _product(i: int, sku: str, **extra) -> dict:
    return {
        "Product": {
            "id": i, "name": sku, "code2": sku, "sell_price": "1000",
            "enabled": True, "exclude_ecommerce": False, **extra,
        }
    }


def world(products: list[dict], stocks: dict[str, float], shopify_skus: list[str], *, shopify_down=False):
    """BIMS serves ``products`` and ``stocks``; Shopify lists ``shopify_skus``."""
    writes: list[str] = []

    def graphql(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "inventorySetQuantities" in body:
            writes.append(body)
            return httpx.Response(200, json={"data": {"inventorySetQuantities": {"userErrors": []}}})
        if shopify_down:
            return httpx.Response(500, json={"errors": "down"})
        return httpx.Response(200, json=_variants(*shopify_skus))

    mock = respx.mock(assert_all_called=False)
    mock.get(BIMS_INDEX).mock(
        side_effect=[
            httpx.Response(200, json={"status": "ok", "count": str(len(products)), "data": products}),
            httpx.Response(200, json={"status": "ok", "count": str(len(products)), "data": []}),
        ]
        * 5
    )
    mock.post(BIMS_STOCK).mock(
        return_value=httpx.Response(
            200,
            json={"status": "OK", "data": {"stockPorSku": [{"sku": s, "stock": q} for s, q in stocks.items()]}},
        )
    )
    mock.post(GRAPHQL).mock(side_effect=graphql)
    return mock, writes


async def _pull(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await do_pull(tenant, session, **kwargs)


async def _outcome(session_factory, tenant_id):
    async with session_factory() as session:
        return (await SqlAlchemySyncStateRepository(session).get_status(tenant_id))["last_pull_outcome"]


async def _audit(session_factory, tenant_id, action):
    async with session_factory() as session:
        return await SqlAlchemyAuditLogger(session).get_latest(action=action, tenant_id=tenant_id)


async def test_pull_lands_a_snapshot_and_never_writes_to_shopify(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    mock, writes = world([_product(1, "A"), _product(2, "B")], {"A": 5, "B": 0}, ["A", "B"])
    with mock:
        result = await _pull(session_factory, tenant)

    assert result["status"] == "ok"
    assert result["snapshot"]["rows_pulled"] == 2
    assert writes == []  # a pull never pushes
    async with session_factory() as session:
        snapshot = await SqlAlchemySnapshotRepository(session).load_current(tenant.id)
    assert {r.sku for r in snapshot.rows} == {"A", "B"}
    assert snapshot.stock["A"].quantity == 5 and snapshot.stock["A"].by_warehouse == {1: 5.0}
    outcome = await _outcome(session_factory, tenant.id)
    assert outcome["status"] == "ok" and outcome["rows"] == 2
    assert (await _audit(session_factory, tenant.id, "snapshot.pull_completed")) is not None


async def test_pull_duration_is_recorded_for_the_pull_interval_floor(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    mock, _ = world([_product(1, "A")], {"A": 1}, ["A"])
    with mock:
        await _pull(session_factory, tenant)
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        assert len(await repo.get_recent_durations(tenant.id, "full")) == 1
        assert await repo.get_recent_durations(tenant.id, "push") == []


async def test_pull_recomputes_the_dashboard_catalog_stats(migrated_db):
    """BIMS eligible, with stock, in Shopify, matching SKUs: no longer 13 days stale."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    products = [
        _product(1, "A"),
        _product(2, "B"),
        _product(3, "C"),
        _product(4, "OFF", enabled=False),
    ]
    mock, _ = world(products, {"A": 5, "B": 0, "C": 2}, ["A", "C", "ONLY-SHOPIFY"])
    with mock:
        result = await _pull(session_factory, tenant)

    entry = await _audit(session_factory, tenant.id, "catalog.reconciliation")
    assert entry is not None
    payload = entry.payload
    assert payload["bims"]["eligible_variants"] == 3  # the disabled row is not eligible
    assert payload["bims"]["with_stock"] == 2  # A and C
    assert payload["shopify"] == {"products": 3, "variants": 3, "variants_with_sku": 3}
    assert payload["reconciliation"]["matched_skus"] == 2  # A, C
    assert payload["reconciliation"]["skus_in_bims_not_in_shopify"] == 1  # B
    assert payload["reconciliation"]["skus_in_shopify_not_in_bims"] == 1
    assert payload["source"] == "pull"
    assert result["stats"]["bims"]["with_stock"] == 2
    async with session_factory() as session:
        meta = await SqlAlchemySnapshotRepository(session).get_current_meta(tenant.id)
    assert meta.stats["reconciliation"]["matched_skus"] == 2


async def test_stats_failure_leaves_the_pull_ok_and_says_so(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    mock, _ = world([_product(1, "A")], {"A": 1}, [], shopify_down=True)
    with mock:
        result = await _pull(session_factory, tenant)
    assert result["status"] == "ok"
    assert result["stats"] is None and result["stats_error"]
    assert (await _audit(session_factory, tenant.id, "catalog.reconciliation")) is None
    assert (await _outcome(session_factory, tenant.id))["stats_error"]


async def test_rejected_pull_is_recorded_loudly_and_keeps_stats_and_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    big = [_product(i, f"S{i}") for i in range(10)]
    mock, _ = world(big, {f"S{i}": 1 for i in range(10)}, [])
    with mock:
        await _pull(session_factory, tenant)
    before = await _audit(session_factory, tenant.id, "catalog.reconciliation")

    small = [_product(i, f"S{i}") for i in range(3)]
    mock, _ = world(small, {f"S{i}": 1 for i in range(3)}, [])
    with mock:
        result = await _pull(session_factory, tenant)

    assert result["status"] == "rejected" and result["reason"] == "too_few_rows"
    outcome = await _outcome(session_factory, tenant.id)
    assert outcome["status"] == "rejected" and outcome["reason"] == "too_few_rows"
    assert outcome["detail"]["previous_rows"] == 10
    rejected = await _audit(session_factory, tenant.id, "snapshot.pull_rejected")
    assert rejected.payload["reason"] == "too_few_rows"
    async with session_factory() as session:
        snapshot = await SqlAlchemySnapshotRepository(session).load_current(tenant.id)
        status = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    assert len(snapshot.rows) == 10
    assert "pull rejected" in status["last_error"]
    # the dashboard stats still describe the last GOOD snapshot
    assert (await _audit(session_factory, tenant.id, "catalog.reconciliation")).id == before.id


async def test_failed_pull_keeps_previous_snapshot_and_records_the_error(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    mock, _ = world([_product(1, "A")], {"A": 1}, ["A"])
    with mock:
        await _pull(session_factory, tenant)

    with respx.mock(assert_all_called=False) as down:
        down.get(BIMS_INDEX).mock(return_value=httpx.Response(200, json={"status": "error", "code": "500", "message": "boom"}))
        with pytest.raises(Exception, match="boom"):
            await _pull(session_factory, tenant)

    outcome = await _outcome(session_factory, tenant.id)
    assert outcome["status"] == "failed" and "boom" in outcome["error"]
    assert (await _audit(session_factory, tenant.id, "snapshot.pull_failed")) is not None
    async with session_factory() as session:
        assert len((await SqlAlchemySnapshotRepository(session).load_current(tenant.id)).rows) == 1


async def test_a_successful_pull_clears_only_a_pull_error(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_last_error(tenant.id, "push: shopify 500")
    mock, _ = world([_product(1, "A")], {"A": 1}, ["A"])
    with mock:
        await _pull(session_factory, tenant)
    async with session_factory() as session:
        status = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    assert status["last_error"] == "push: shopify 500"


async def test_record_skip_is_visible(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await record_skip(session_factory, "pull", tenant)

    outcome = await _outcome(session_factory, tenant.id)
    assert outcome["status"] == "skipped"
    assert "still running" in outcome["reason"]
    skipped = await _audit(session_factory, tenant.id, "snapshot.pull_skipped")
    assert skipped is not None
