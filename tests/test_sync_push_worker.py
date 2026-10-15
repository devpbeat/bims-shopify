"""do_push: the PUSH worker. Reads the snapshot, pushes differences to Shopify,
never calls BIMS, and refuses to push from a snapshot it cannot trust."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.ops.sync_workers import do_push
from tests.sync_world import ShopifyWorld, land_snapshot


async def _push(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await do_push(tenant, session, **kwargs)


async def _state(session_factory, tenant_id):
    async with session_factory() as session:
        return await SqlAlchemySyncStateRepository(session).get_status(tenant_id)


async def _audits(session_factory, tenant_id, action):
    async with session_factory() as session:
        return await SqlAlchemyAuditLogger(session).list_recent_by_action(
            action=action, tenant_id=tenant_id, limit=50
        )


async def _set_hashes(session_factory, tenant_id, hashes):
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_product_hashes(tenant_id, hashes)


# -- it pushes from the snapshot and never calls BIMS -----------------------------


async def test_push_sends_the_snapshot_differences_and_never_calls_bims(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 3})
    world = ShopifyWorld(["A", "B"])
    with world:
        result = await _push(session_factory, tenant)

    assert result["status"] == "ok"
    assert world.pushed == {"A": 5, "B": 3}
    assert world.bims_calls == 0
    state = await _state(session_factory, tenant.id)
    assert state["last_run_summary"]["status"] == "ok"
    assert state["last_run_summary"]["snapshot"]["rows"] == 2
    assert state["last_push_outcome"]["status"] == "ok"
    assert state["last_run_at"] is not None


async def test_push_only_pushes_what_differs_from_the_last_push(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 3})
    await _set_hashes(session_factory, tenant.id, {"A": "5", "B": "1"})
    world = ShopifyWorld(["A", "B"])
    with world:
        result = await _push(session_factory, tenant)
    assert world.pushed == {"B": 3}
    assert result["synced_deltas"] == 1
    assert result["report"]["unchanged"]["count"] == 1


async def test_the_last_pushed_baseline_advances_for_known_skus(migrated_db):
    """Regression: the stored baseline used to keep the OLD value for every SKU
    it already knew, so a changed SKU was re-pushed forever and a SKU returning
    to its old value was never corrected."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"])
    with world:
        await land_snapshot(session_factory, tenant, {"A": 5})
        await _push(session_factory, tenant, skip_unchanged=False)
        await land_snapshot(session_factory, tenant, {"A": 7})
        await _push(session_factory, tenant, skip_unchanged=False)
        assert world.pushed == {"A": 7}
        calls = world.write_calls

        again = await _push(session_factory, tenant, skip_unchanged=False)  # same data
        assert again["synced_deltas"] == 0 and world.write_calls == calls

        await land_snapshot(session_factory, tenant, {"A": 5})  # back to the first value
        await _push(session_factory, tenant, skip_unchanged=False)
        assert world.pushed == {"A": 5}


# -- it declines instead of pushing from a snapshot it cannot trust ------------


async def test_declines_without_a_snapshot_and_says_why(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"])
    with world:
        result = await _push(session_factory, tenant)

    assert result["status"] == "push_declined" and result["reason"] == "missing"
    assert world.write_calls == 0 and world.listing_calls == 0 and world.bims_calls == 0
    outcome = (await _state(session_factory, tenant.id))["last_push_outcome"]
    assert outcome["status"] == "declined" and outcome["reason"] == "missing"
    assert len(await _audits(session_factory, tenant.id, "sync.push_declined")) == 1


async def test_declines_a_snapshot_older_than_the_tenant_limit(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(snapshot_max_age_minutes=60)
    old = datetime.now(UTC) - timedelta(minutes=61)
    await land_snapshot(session_factory, tenant, {"A": 5}, finished_at=old)
    world = ShopifyWorld(["A"])
    with world:
        result = await _push(session_factory, tenant)

    assert result["status"] == "push_declined" and result["reason"] == "stale"
    assert result["detail"]["max_age_minutes"] == 60
    assert result["detail"]["age_minutes"] >= 61
    assert world.write_calls == 0
    audit = (await _audits(session_factory, tenant.id, "sync.push_declined"))[0]
    assert audit.payload["reason"] == "stale"


async def test_pushes_from_a_snapshot_just_inside_the_limit(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(snapshot_max_age_minutes=60)
    await land_snapshot(
        session_factory, tenant, {"A": 5}, finished_at=datetime.now(UTC) - timedelta(minutes=59)
    )
    world = ShopifyWorld(["A"])
    with world:
        result = await _push(session_factory, tenant)
    assert result["status"] == "ok" and world.pushed == {"A": 5}


async def test_the_staleness_limit_is_read_from_the_tenant_each_time(migrated_db):
    from dataclasses import replace

    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(snapshot_max_age_minutes=600)
    await land_snapshot(
        session_factory, tenant, {"A": 5}, finished_at=datetime.now(UTC) - timedelta(minutes=90)
    )
    world = ShopifyWorld(["A"])
    with world:
        tight = await _push(session_factory, replace(tenant, snapshot_max_age_minutes=60))
        loose = await _push(session_factory, tenant)
    assert tight["status"] == "push_declined"
    assert loose["status"] == "ok"


async def test_declines_a_snapshot_flagged_incomplete(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5}, complete=False)
    world = ShopifyWorld(["A"])
    with world:
        result = await _push(session_factory, tenant)
    assert result["status"] == "push_declined" and result["reason"] == "incomplete"
    assert world.write_calls == 0


async def test_an_unknown_completeness_snapshot_is_usable(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5}, complete=None)
    world = ShopifyWorld(["A"])
    with world:
        assert (await _push(session_factory, tenant))["status"] == "ok"


async def test_repeating_the_same_decline_is_not_audited_again(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"])
    with world:
        for _ in range(3):
            await _push(session_factory, tenant)
    assert len(await _audits(session_factory, tenant.id, "sync.push_declined")) == 1
    outcome = (await _state(session_factory, tenant.id))["last_push_outcome"]
    assert outcome["first_seen_at"] <= outcome["at"]


# -- the safety rules survive the split ------------------------------------------


async def test_absent_stock_is_unknown_and_never_pushed_as_zero(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5}, no_stock=("GHOST",))
    world = ShopifyWorld(["A", "GHOST"])
    with world:
        result = await _push(session_factory, tenant)

    assert world.pushed == {"A": 5}  # GHOST was never written
    assert result["report"]["not_in_bims"]["count"] == 1
    assert result["report"]["not_in_bims"]["sample"] == [{"sku": "GHOST"}]


async def test_absent_stock_pushes_zero_only_when_the_tenant_opts_in(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"absent_stock_means_zero": True})
    live = {f"A{i}": 5 for i in range(10)}  # enough live SKUs to stay under the zero guard
    await land_snapshot(session_factory, tenant, live, no_stock=("GHOST",))
    world = ShopifyWorld([*live, "GHOST"])
    with world:
        await _push(session_factory, tenant)
    assert world.pushed == {**live, "GHOST": 0}


async def test_mass_zero_guard_still_blocks_and_nothing_reaches_shopify(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    skus = [f"S{i}" for i in range(10)]
    await _set_hashes(session_factory, tenant.id, {s: "5" for s in skus})
    await land_snapshot(session_factory, tenant, {s: 0 for s in skus})
    world = ShopifyWorld(skus)
    with world:
        result = await _push(session_factory, tenant)

    assert result["status"] == "needs_confirmation" and result["reason"] == "zero_ratio_exceeded"
    assert world.write_calls == 0
    assert result["report"]["guard"]["fired"] is True
    assert result["report"]["guard"]["zero_count"] == 10
    assert len(result["report"]["blocked_by_guard"]["sample"]) > 0
    state = await _state(session_factory, tenant.id)
    assert state["last_run_at"] is None  # the watermark did not advance
    assert state["last_push_outcome"]["status"] == "needs_confirmation"


async def test_a_blocked_push_is_audited_once_per_snapshot_not_once_per_tick(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    skus = [f"S{i}" for i in range(10)]
    await _set_hashes(session_factory, tenant.id, {s: "5" for s in skus})
    await land_snapshot(session_factory, tenant, {s: 0 for s in skus})
    world = ShopifyWorld(skus)
    with world:
        for _ in range(4):
            await _push(session_factory, tenant)
    assert len(await _audits(session_factory, tenant.id, "sync.needs_confirmation")) == 1


async def test_force_bypasses_the_guard_like_before(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    skus = [f"S{i}" for i in range(10)]
    await _set_hashes(session_factory, tenant.id, {s: "5" for s in skus})
    await land_snapshot(session_factory, tenant, {s: 0 for s in skus})
    world = ShopifyWorld(skus)
    with world:
        result = await _push(session_factory, tenant, force=True)
    assert result["status"] == "ok"
    assert world.pushed == {s: 0 for s in skus}


async def test_detail_report_buckets_describe_the_push(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    ups = {f"UP{i}": 8 for i in range(5)}
    await land_snapshot(
        session_factory, tenant, {**ups, "SAME": 4, "ZERO": 0}, no_stock=("NOSTOCK",)
    )
    await _set_hashes(session_factory, tenant.id, {"SAME": "4"})
    world = ShopifyWorld([*ups, "SAME", "ZERO", "SHOPIFY-ONLY"])  # NOSTOCK: no Shopify variant
    with world:
        result = await _push(session_factory, tenant)

    report = result["report"]
    assert report["mode"] == "full" and report["full_scan"] is True
    assert report["scanned"] == {"bims": 8, "shopify": 8}
    assert result["scanned"] == report["scanned"]
    assert report["updated"]["count"] == 6  # UP0-4 and ZERO
    assert report["unchanged"]["count"] == 1  # SAME
    assert report["zero_stock"]["count"] == 1
    assert report["not_in_shopify"]["count"] == 1  # NOSTOCK has no variant
    assert report["not_in_bims"]["count"] == 1  # SHOPIFY-ONLY
    assert report["failed"]["count"] == 0
    assert report["guard"]["fired"] is False


# -- bookkeeping -------------------------------------------------------------------


async def test_push_duration_goes_to_the_push_history_not_the_pull_history(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"])
    with world:
        await _push(session_factory, tenant)
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        assert len(await repo.get_recent_durations(tenant.id, "push")) == 1
        assert await repo.get_recent_durations(tenant.id, "full") == []


async def test_a_second_scheduled_push_of_the_same_snapshot_does_nothing(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"])
    with world:
        first = await _push(session_factory, tenant)
        listings = world.listing_calls
        second = await _push(session_factory, tenant)
        forced_by_oneshot = await _push(session_factory, tenant, skip_unchanged=False)
    assert first["status"] == "ok"
    assert second["status"] == "unchanged"
    assert world.listing_calls == listings + 1  # only the one-shot looked at Shopify again
    assert forced_by_oneshot["status"] == "ok"
    outcome = (await _state(session_factory, tenant.id))["last_push_outcome"]
    assert outcome["status"] == "ok"
    pushes = [e for e in await _audits(session_factory, tenant.id, "sync.completed")]
    assert len(pushes) == 2  # first push + one-shot; the skipped tick wrote nothing


async def test_a_new_snapshot_is_pushed_after_an_unchanged_one(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"])
    with world:
        await land_snapshot(session_factory, tenant, {"A": 5})
        await _push(session_factory, tenant)
        await land_snapshot(session_factory, tenant, {"A": 9})
        result = await _push(session_factory, tenant)
    assert result["status"] == "ok" and world.pushed == {"A": 9}


async def test_dry_run_reports_without_writing_or_advancing_anything(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"])
    with world:
        result = await _push(session_factory, tenant, dry_run=True)
    assert result["dry_run"] is True and result["mode"] == "full"
    assert result["report"]["would_update"] == 1
    assert world.write_calls == 0
    state = await _state(session_factory, tenant.id)
    assert state["last_run_at"] is None
    async with session_factory() as session:
        assert await SqlAlchemySyncStateRepository(session).get_product_hashes(tenant.id) == {}


async def test_shopify_failure_is_recorded_and_the_baseline_is_not_advanced(
    migrated_db, monkeypatch
):
    import asyncio

    real_sleep = asyncio.sleep

    async def no_backoff(_seconds):  # the Shopify client retries 5xx with backoff
        await real_sleep(0)

    monkeypatch.setattr("bims_shopify.adapters.shopify.client.asyncio.sleep", no_backoff)
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"], fail_writes=True)
    with world, pytest.raises(Exception):  # noqa: B017 - the Shopify client's own error type
        await _push(session_factory, tenant, skip_unchanged=False)

    state = await _state(session_factory, tenant.id)
    assert state["last_error"].startswith("push: ")
    assert state["last_push_outcome"]["status"] == "failed"
    assert state["last_run_at"] is None
    async with session_factory() as session:
        assert await SqlAlchemySyncStateRepository(session).get_product_hashes(tenant.id) == {}
    assert len(await _audits(session_factory, tenant.id, "sync.error")) == 1
