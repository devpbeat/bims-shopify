"""do_reconcile: compare Shopify's REAL quantities with the snapshot and push
every SKU that differs, ignoring the stored baseline. Same safety rules as the
push: guard, unknown stays unknown, no usable snapshot -> decline."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.ops.sync_workers import do_push, do_reconcile
from tests.sync_world import ShopifyWorld, land_snapshot


async def _reconcile(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await do_reconcile(tenant, session, **kwargs)


async def _push(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await do_push(tenant, session, **kwargs)


async def _state(session_factory, tenant_id):
    async with session_factory() as session:
        return await SqlAlchemySyncStateRepository(session).get_status(tenant_id)


async def _hashes(session_factory, tenant_id):
    async with session_factory() as session:
        return await SqlAlchemySyncStateRepository(session).get_product_hashes(tenant_id)


async def _set_hashes(session_factory, tenant_id, hashes):
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_product_hashes(tenant_id, hashes)


# -- the reported bug -------------------------------------------------------------


async def test_reconcile_corrects_a_sku_the_baseline_wrongly_says_is_in_sync(migrated_db):
    """Regression: the push diffs against what it LAST PUSHED, so a SKU that
    drifted in Shopify (hand edit, half-failed push, ignored mutation) was
    frozen forever. Reconcile compares against what Shopify actually holds."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 3})
    await _set_hashes(session_factory, tenant.id, {"A": "5", "B": "3"})  # baseline: in sync
    world = ShopifyWorld(["A", "B"], levels={"A": 2, "B": 3})  # but Shopify drifted on A
    with world:
        pushed = await _push(session_factory, tenant, skip_unchanged=False)
        assert pushed["synced_deltas"] == 0 and world.write_calls == 0  # the bug: frozen

        result = await _reconcile(session_factory, tenant)

    assert result["status"] == "ok"
    assert world.pushed == {"A": 5}  # only the drifted SKU; B was already right
    assert world.levels == {"A": 5, "B": 3}
    assert result["drift"]["count"] == 1
    assert world.bims_calls == 0


async def test_reconcile_refreshes_the_baseline_from_what_shopify_now_holds(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 3, "C": 9})
    # A: baseline right, Shopify wrong. B: baseline WRONG, Shopify right. C: unknown to baseline.
    await _set_hashes(session_factory, tenant.id, {"A": "5", "B": "1", "OLD": "4"})
    world = ShopifyWorld(["A", "B", "C"], levels={"A": 2, "B": 3, "C": 0})
    with world:
        await _reconcile(session_factory, tenant)
        # the cheap incremental push that follows now has nothing to do
        follow_up = await _push(session_factory, tenant, skip_unchanged=False)

    hashes = await _hashes(session_factory, tenant.id)
    assert {k: float(v) for k, v in hashes.items()} == {"A": 5.0, "B": 3.0, "C": 9.0, "OLD": 4.0}
    assert follow_up["synced_deltas"] == 0


# -- drift report -----------------------------------------------------------------


async def test_drift_count_amounts_and_bounded_sample_are_reported(migrated_db, monkeypatch):
    from bims_shopify.application import reconcile_inventory

    monkeypatch.setattr(reconcile_inventory, "DRIFT_SAMPLE_SIZE", 3)
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    skus = [f"S{i}" for i in range(10)]
    await land_snapshot(session_factory, tenant, {sku: 10 for sku in skus}, )
    world = ShopifyWorld(skus, levels={sku: 4 for sku in skus})  # all 10 are 6 short
    with world:
        result = await _reconcile(session_factory, tenant)

    drift = result["drift"]
    assert drift["count"] == 10
    assert drift["total_abs_delta"] == 60 and drift["max_abs_delta"] == 6
    assert len(drift["sample"]) == 3  # bounded, the count is not
    assert set(drift["sample"][0]) >= {"sku", "shopify", "snapshot"}
    assert drift["sample"][0]["shopify"] == 4 and drift["sample"][0]["snapshot"] == 10
    assert result["report"]["updated"]["count"] == 10


async def test_a_healthy_system_reconciles_to_zero_drift_and_writes_nothing(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 0})
    world = ShopifyWorld(["A", "B"], levels={"A": 5, "B": 0})
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["status"] == "ok"
    assert result["drift"]["count"] == 0 and result["drift"]["sample"] == []
    assert result["report"]["in_sync"]["count"] == 2
    assert world.write_calls == 0


async def test_the_outcome_is_recorded_apart_from_the_push_outcome(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"], levels={"A": 1})
    with world:
        await _push(session_factory, tenant, skip_unchanged=False)  # a push outcome exists
        push_outcome = (await _state(session_factory, tenant.id))["last_push_outcome"]
        world.levels["A"] = 1  # drift again after the push
        await _reconcile(session_factory, tenant)

    state = await _state(session_factory, tenant.id)
    assert state["last_push_outcome"] == push_outcome  # untouched
    outcome = state["last_reconcile_outcome"]
    assert outcome["status"] == "ok" and outcome["trigger"] == "scheduled"
    assert outcome["drift"]["count"] == 1 and outcome["drift"]["sample"][0]["sku"] == "A"
    assert outcome["updated"] == 1 and outcome["snapshot_id"] is not None
    async with session_factory() as session:
        audits = await SqlAlchemyAuditLogger(session).list_recent_by_action(
            action="sync.reconcile_completed", tenant_id=tenant.id, limit=5
        )
    assert audits and audits[0].payload["drift"]["count"] == 1


# -- no level vs zero, unknown stock ---------------------------------------------


async def test_no_level_is_distinct_from_zero_and_never_gets_a_phantom_zero(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(
        session_factory,
        tenant,
        {"NOLEVEL_ZERO": 0, "NOLEVEL_STOCK": 4, "ZERO_OK": 0, "ZERO_DRIFT": 3},
    )
    world = ShopifyWorld(
        ["NOLEVEL_ZERO", "NOLEVEL_STOCK", "ZERO_OK", "ZERO_DRIFT"],
        levels={"NOLEVEL_ZERO": None, "NOLEVEL_STOCK": None, "ZERO_OK": 0, "ZERO_DRIFT": 0},
    )
    with world:
        result = await _reconcile(session_factory, tenant)

    # no level + BIMS zero: nothing to do, and certainly no zero written
    assert "NOLEVEL_ZERO" not in world.pushed
    assert world.levels["NOLEVEL_ZERO"] is None
    # no level + BIMS stock: pushed (the write activates the item), as real stock
    assert world.pushed["NOLEVEL_STOCK"] == 4
    # a stocked zero IS a value: ZERO_OK is in sync, ZERO_DRIFT is real drift reported as 0
    assert "ZERO_OK" not in world.pushed and world.pushed["ZERO_DRIFT"] == 3
    drift = {row["sku"]: row for row in result["drift"]["sample"]}
    assert result["drift"]["count"] == 1 and drift["ZERO_DRIFT"]["shopify"] == 0
    no_level = result["report"]["no_level"]
    assert no_level["count"] == 2 and no_level["pushed"] == 1 and no_level["skipped_zero"] == 1
    rows = {row["sku"]: row for row in no_level["sample"]}
    assert rows["NOLEVEL_STOCK"]["shopify"] is None and rows["NOLEVEL_ZERO"]["shopify"] is None


async def test_absent_bims_stock_is_unknown_and_never_pushed_as_zero(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5}, no_stock=("UNKNOWN",))
    world = ShopifyWorld(["A", "UNKNOWN"], levels={"A": 5, "UNKNOWN": 7})
    with world:
        result = await _reconcile(session_factory, tenant)

    assert world.write_calls == 0
    assert world.levels["UNKNOWN"] == 7  # Shopify's number is left alone
    assert result["report"]["unknown_stock"]["count"] == 1
    assert result["drift"]["count"] == 0


async def test_skus_bims_has_but_shopify_lacks_are_reported_not_pushed(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "GHOST": 2})
    world = ShopifyWorld(["A"], levels={"A": 5})
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["report"]["not_in_shopify"]["count"] == 1
    assert world.write_calls == 0


async def test_fractional_bims_stock_is_compared_as_the_integer_that_would_be_pushed(migrated_db):
    """The push truncates to an integer, so 2.5 vs a Shopify 2 is not drift:
    otherwise it would 'drift' on every run and never settle."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 2.5})
    world = ShopifyWorld(["A"], levels={"A": 2})
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["drift"]["count"] == 0 and world.write_calls == 0


# -- the safety guard --------------------------------------------------------------


async def test_the_mass_zero_guard_still_fires_and_nothing_is_written(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    skus = [f"S{i}" for i in range(10)]
    await land_snapshot(session_factory, tenant, {sku: 0 for sku in skus})  # a "bad" snapshot
    await _set_hashes(session_factory, tenant.id, {"S0": "5"})
    world = ShopifyWorld(skus, levels={sku: 5 for sku in skus})
    with world:
        result = await _reconcile(session_factory, tenant)

    assert result["status"] == "needs_confirmation"
    assert result["reason"] == "zero_ratio_exceeded"
    assert world.write_calls == 0 and world.levels == {sku: 5 for sku in skus}
    assert result["drift"]["count"] == 10  # the drift is still reported
    assert result["report"]["guard"]["fired"] is True
    assert await _hashes(session_factory, tenant.id) == {"S0": "5"}  # baseline untouched
    outcome = (await _state(session_factory, tenant.id))["last_reconcile_outcome"]
    assert outcome["status"] == "needs_confirmation" and outcome["drift"]["count"] == 10


async def test_the_max_changed_guard_fires_on_a_huge_drift(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"max_changed_variants": 2})
    skus = ["A", "B", "C"]
    await land_snapshot(session_factory, tenant, {sku: 9 for sku in skus})
    world = ShopifyWorld(skus, levels={sku: 1 for sku in skus})
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["status"] == "needs_confirmation" and result["reason"] == "max_changed_exceeded"
    assert world.write_calls == 0


async def test_force_is_the_explicit_override_of_the_guard(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"max_changed_variants": 2})
    skus = ["A", "B", "C"]
    await land_snapshot(session_factory, tenant, {sku: 9 for sku in skus})
    world = ShopifyWorld(skus, levels={sku: 1 for sku in skus})
    with world:
        result = await _reconcile(session_factory, tenant, force=True)
    assert result["status"] == "ok" and world.levels == {sku: 9 for sku in skus}


# -- it refuses a snapshot it cannot trust -----------------------------------------


async def test_declines_without_a_snapshot_and_never_reads_shopify(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = ShopifyWorld(["A"], levels={"A": 1})
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["status"] == "reconcile_declined" and result["reason"] == "missing"
    assert world.inventory_reads == 0 and world.write_calls == 0
    outcome = (await _state(session_factory, tenant.id))["last_reconcile_outcome"]
    assert outcome["status"] == "declined" and outcome["reason"] == "missing"


async def test_declines_an_incomplete_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5}, complete=False)
    world = ShopifyWorld(["A"], levels={"A": 1})
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["status"] == "reconcile_declined" and result["reason"] == "incomplete"
    assert world.inventory_reads == 0 and world.write_calls == 0


async def test_declines_a_stale_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(snapshot_max_age_minutes=60)
    await land_snapshot(
        session_factory, tenant, {"A": 5}, finished_at=datetime.now(UTC) - timedelta(minutes=90)
    )
    world = ShopifyWorld(["A"], levels={"A": 1})
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["status"] == "reconcile_declined" and result["reason"] == "stale"
    assert world.inventory_reads == 0 and world.write_calls == 0


async def test_a_snapshot_that_goes_stale_during_the_scan_is_not_pushed(migrated_db, monkeypatch):
    """The scan of a big catalog takes a while: the snapshot is judged again
    right before writing, not only when the run starts."""
    from bims_shopify.ops import sync_workers

    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(snapshot_max_age_minutes=60)
    await land_snapshot(session_factory, tenant, {"A": 5})
    clock = {"offset": timedelta(0)}
    monkeypatch.setattr(sync_workers, "_now", lambda: datetime.now(UTC) + clock["offset"])
    world = ShopifyWorld(["A"], levels={"A": 1})
    world.after_inventory_read = lambda: clock.update(offset=timedelta(minutes=90))
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["status"] == "reconcile_declined" and result["reason"] == "stale"
    assert world.write_calls == 0


# -- dry run and failure -------------------------------------------------------------


async def test_dry_run_reports_the_drift_without_writing_or_touching_the_baseline(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    await _set_hashes(session_factory, tenant.id, {"A": "5"})
    world = ShopifyWorld(["A"], levels={"A": 2})
    with world:
        result = await _reconcile(session_factory, tenant, dry_run=True)
    assert result["dry_run"] is True and result["drift"]["count"] == 1
    assert world.write_calls == 0 and world.levels == {"A": 2}
    assert await _hashes(session_factory, tenant.id) == {"A": "5"}


async def test_dry_run_still_reports_when_the_guard_would_have_fired(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"max_changed_variants": 1})
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 5})
    world = ShopifyWorld(["A", "B"], levels={"A": 1, "B": 1})
    with world:
        result = await _reconcile(session_factory, tenant, dry_run=True)
    assert result["dry_run"] is True and result["guard_fired"] is True
    assert result["would_update"] == 2 and world.write_calls == 0


async def test_a_failed_write_is_recorded_and_leaves_the_baseline_alone(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    await _set_hashes(session_factory, tenant.id, {"A": "1"})
    world = ShopifyWorld(["A"], levels={"A": 2}, fail_writes=True)
    with world, pytest.raises(Exception):  # noqa: B017 - any transport failure
        await _reconcile(session_factory, tenant)
    assert await _hashes(session_factory, tenant.id) == {"A": "1"}
    outcome = (await _state(session_factory, tenant.id))["last_reconcile_outcome"]
    assert outcome["status"] == "failed" and "error" in outcome


async def test_a_mutation_shopify_ignores_shows_up_as_drift_again_next_run(migrated_db):
    """The point of the metric: writes silently dropped upstream keep reporting."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"], levels={"A": 2}, ignore_writes=True)
    with world:
        first = await _reconcile(session_factory, tenant)
        second = await _reconcile(session_factory, tenant)
    assert first["drift"]["count"] == 1 and second["drift"]["count"] == 1
