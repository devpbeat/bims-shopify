"""The one-shot sync (POST /sync/{slug}/run, the `sync` ops command) is
"pull now, then push now" over the same two workers, not a third code path."""
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


async def test_oneshot_pulls_from_bims_then_pushes_to_shopify_end_to_end(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A", "B"])
    world.serve_bims({"A": 5, "B": 3})
    with world:
        result = await _oneshot(session_factory, tenant)

    assert result["status"] == "ok"
    assert world.pushed == {"A": 5, "B": 3}
    assert "index" in world.bims_requests and "stock" in world.bims_requests
    assert result["pull"]["status"] == "ok" and result["pull"]["snapshot"]["rows_pulled"] == 2
    assert result["scanned"] == {"bims": 2, "shopify": 2}
    assert result["mode"] == "full"
    async with session_factory() as session:
        snapshot = await SqlAlchemySnapshotRepository(session).load_current(tenant.id)
        state = await SqlAlchemySyncStateRepository(session).get_status(tenant.id)
    assert {r.sku for r in snapshot.rows} == {"A", "B"}
    assert state["last_run_summary"]["status"] == "ok"
    assert state["last_pull_outcome"]["trigger"] == "manual"


async def test_oneshot_is_literally_do_pull_then_do_push(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    calls: list[tuple[str, dict]] = []

    async def fake_pull(t, session, **kwargs):
        calls.append(("pull", kwargs))
        return {"status": "ok", "snapshot": {"rows_pulled": 1}}

    async def fake_push(t, session, **kwargs):
        calls.append(("push", kwargs))
        return {"status": "ok", "mode": "full", "scanned": {"bims": 1, "shopify": 1}}

    monkeypatch.setattr(sync_workers, "do_pull", fake_pull)
    monkeypatch.setattr(sync_workers, "do_push", fake_push)

    result = await _oneshot(session_factory, tenant, force=True, full=True)

    assert [name for name, _ in calls] == ["pull", "push"]
    push_kwargs = calls[1][1]
    assert push_kwargs["force"] is True and push_kwargs["dry_run"] is False
    assert push_kwargs["skip_unchanged"] is False  # an operator asked: always compare
    assert push_kwargs["trigger"] == "manual"
    assert result["status"] == "ok" and result["pull"]["status"] == "ok"


async def test_oneshot_always_compares_even_when_the_snapshot_was_already_pushed(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"])
    world.serve_bims({"A": 5})
    with world:
        await _oneshot(session_factory, tenant)
        listings = world.listing_calls
        again = await _oneshot(session_factory, tenant)
    assert again["status"] == "ok"
    assert world.listing_calls > listings  # it really compared again


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
    assert world.write_calls == 0 and world.listing_calls == 0  # nothing reached Shopify


async def test_oneshot_dry_run_keeps_its_old_shape_and_still_refreshes_the_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"])
    world.serve_bims({"A": 5})
    with world:
        result = await _oneshot(session_factory, tenant, dry_run=True)
    assert result["dry_run"] is True and result["mode"] == "full"
    assert result["report"]["would_update"] == 1
    assert result["pull"]["status"] == "ok"
    assert world.write_calls == 0
    async with session_factory() as session:
        assert await SqlAlchemySnapshotRepository(session).get_current_meta(tenant.id) is not None


async def test_oneshot_guard_still_blocks_a_mass_zero(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    skus = [f"S{i}" for i in range(10)]
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_product_hashes(
            tenant.id, {s: "5" for s in skus}
        )
    world = ShopifyWorld(skus)
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
