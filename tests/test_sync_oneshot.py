"""The one-shot sync (POST /sync/{slug}/run, the `sync` ops command) is
"pull now, then reconcile now" over the same two workers, not a third code path."""
from __future__ import annotations

import pytest

from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.api.sync import _do_run_sync
from bims_shopify.ops import sync_workers
from tests.sync_world import ShopifyWorld, land_snapshot


async def _oneshot(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await _do_run_sync(tenant, kwargs.pop("dry_run", False), session, **kwargs)


async def test_oneshot_pulls_from_bims_then_reconciles_shopify_end_to_end(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A", "B"], levels={"A": 0, "B": 0})
    world.serve_bims({"A": 5, "B": 3})
    with world:
        result = await _oneshot(session_factory, tenant)

    assert result["status"] == "ok"
    assert world.pushed == {"A": 5, "B": 3}
    assert "index" in world.bims_requests and "stock" in world.bims_requests
    assert result["pull"]["status"] == "ok" and result["pull"]["snapshot"]["rows_pulled"] == 2
    assert result["scanned"] == {"snapshot": 2, "shopify": 2}
    assert result["mode"] == "reconcile" and result["drift"]["count"] == 2
    async with session_factory() as session:
        snapshot = await SqlAlchemySnapshotRepository(session).load_current(tenant.id)
        state = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    assert {r.sku for r in snapshot.rows} == {"A", "B"}
    assert state["last_run_summary"]["status"] == "ok"
    assert state["last_pull_outcome"]["trigger"] == "manual"


async def test_oneshot_is_literally_do_pull_then_do_reconcile(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    calls: list[tuple[str, dict]] = []

    async def fake_pull(t, session, **kwargs):
        calls.append(("pull", kwargs))
        return {"status": "ok", "snapshot": {"rows_pulled": 1}}

    async def fake_reconcile(t, session, **kwargs):
        calls.append(("reconcile", kwargs))
        return {"status": "ok", "mode": "reconcile", "scanned": {"snapshot": 1, "shopify": 1}}

    monkeypatch.setattr(sync_workers, "do_pull", fake_pull)
    monkeypatch.setattr(sync_workers, "do_reconcile", fake_reconcile)

    result = await _oneshot(session_factory, tenant, force=True, full=True)

    assert [name for name, _ in calls] == ["pull", "reconcile"]
    write_kwargs = calls[1][1]
    assert write_kwargs["force"] is True and write_kwargs["dry_run"] is False
    assert write_kwargs["trigger"] == "manual"
    assert result["status"] == "ok" and result["pull"]["status"] == "ok"


async def test_oneshot_always_compares_against_what_shopify_holds_now(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"], levels={"A": 0})
    world.serve_bims({"A": 5})
    with world:
        await _oneshot(session_factory, tenant)
        world.levels["A"] = 1  # someone edited it in the Shopify admin meanwhile
        reads = world.inventory_reads
        again = await _oneshot(session_factory, tenant)
    assert again["status"] == "ok" and again["drift"]["count"] == 1
    assert world.inventory_reads > reads and world.levels["A"] == 5  # it really compared again


async def test_a_rejected_pull_stops_the_oneshot_before_it_pushes(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    skus = [f"S{i}" for i in range(10)]
    await land_snapshot(session_factory, tenant, {s: 4 for s in skus})
    world = ShopifyWorld(skus)
    world.serve_bims({"S0": 1, "S1": 1, "S2": 1})  # BIMS suddenly returns 3 of 10
    with world:
        result = await _oneshot(session_factory, tenant)

    assert result["status"] == "pull_rejected"
    assert result["reason"] == "too_few_rows"
    assert result["pull"]["status"] == "rejected"
    assert world.write_calls == 0 and world.inventory_reads == 0  # nothing reached Shopify


async def test_oneshot_dry_run_reports_without_writing_and_still_refreshes_the_snapshot(
    migrated_db,
):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"], levels={"A": 0})
    world.serve_bims({"A": 5})
    with world:
        result = await _oneshot(session_factory, tenant, dry_run=True)
    assert result["dry_run"] is True and result["mode"] == "reconcile"
    assert result["would_update"] == 1
    assert result["pull"]["status"] == "ok"
    assert world.write_calls == 0
    async with session_factory() as session:
        assert await SqlAlchemySnapshotRepository(session).get_current_meta(tenant.id) is not None


async def test_oneshot_guard_still_blocks_a_mass_zero(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    skus = [f"S{i}" for i in range(10)]
    world = ShopifyWorld(skus, levels={s: 5 for s in skus})
    world.serve_bims({s: 0 for s in skus})
    with world:
        result = await _oneshot(session_factory, tenant)
    assert result["status"] == "needs_confirmation"
    assert world.write_calls == 0


async def test_oneshot_propagates_a_bims_outage_and_keeps_the_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"])  # BIMS down
    with world, pytest.raises(Exception):  # noqa: B017 - BIMS client's own error type
        await _oneshot(session_factory, tenant)
    async with session_factory() as session:
        assert len((await SqlAlchemySnapshotRepository(session).load_current(tenant.id)).rows) == 1
