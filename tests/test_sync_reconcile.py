"""do_reconcile: THE write path for stock. Compare Shopify's REAL quantities with
the snapshot and write every SKU that differs. There is no stored "last pushed"
baseline to drift away from Shopify. Safety rules: mass-zero guard, unknown
stays unknown, no usable snapshot -> decline."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bims_shopify.application.sync_inventory import GUARD_MIN_SAMPLE
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.ops.sync_workers import do_reconcile
from tests.sync_world import ShopifyWorld, land_snapshot


async def _reconcile(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await do_reconcile(tenant, session, **kwargs)


async def _state(session_factory, tenant_id):
    async with session_factory() as session:
        return await SqlAlchemySyncStateRepository(session).get_status(tenant_id)


# -- the old frozen-SKU bug cannot exist ---------------------------------------


async def test_a_drifted_sku_is_corrected_with_no_stored_state_to_mislead_it(migrated_db):
    """Regression for the bug the baseline caused: a SKU that drifted in Shopify (hand
    edit, half-failed write, ignored mutation) was frozen forever because the old
    write path compared the snapshot with what it LAST SENT. Every cycle now compares
    with what Shopify actually holds."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 3})
    world = ShopifyWorld(["A", "B"], levels={"A": 2, "B": 3})
    with world:
        result = await _reconcile(session_factory, tenant)

    assert result["status"] == "ok"
    assert world.pushed == {"A": 5}  # only the drifted SKU; B was already right
    assert world.levels == {"A": 5, "B": 3}
    assert result["drift"]["count"] == 1
    assert world.bims_calls == 0


async def test_there_is_no_baseline_storage_left_to_resurrect(migrated_db):
    from bims_shopify.adapters.persistence.models import SyncStateModel

    assert not hasattr(SyncStateModel, "product_hashes")
    assert not hasattr(SyncStateModel, "last_push_outcome")
    assert not hasattr(SqlAlchemySyncStateRepository, "get_product_hashes")
    assert not hasattr(SqlAlchemySyncStateRepository, "set_product_hashes")


async def test_an_ignored_mutation_cannot_freeze_a_sku_it_is_retried_every_cycle(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"], levels={"A": 2}, ignore_writes=True)
    with world:
        for _ in range(3):
            await _reconcile(session_factory, tenant)
    assert world.write_calls == 3  # never "remembered as sent": it is written again each cycle


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


async def test_the_outcome_summary_and_duration_are_recorded_every_cycle(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"], levels={"A": 1})
    with world:
        await _reconcile(session_factory, tenant)

    state = await _state(session_factory, tenant.id)
    outcome = state["last_reconcile_outcome"]
    assert outcome["status"] == "ok" and outcome["trigger"] == "scheduled"
    assert outcome["drift"]["count"] == 1 and outcome["drift"]["sample"][0]["sku"] == "A"
    assert outcome["updated"] == 1 and outcome["snapshot_id"] is not None
    # What the dashboard reads
    assert state["last_run_at"] is not None
    summary = state["last_run_summary"]
    assert summary["status"] == "ok" and summary["updated"] == 1 and summary["ran_at"]
    async with session_factory() as session:
        repo = SqlAlchemySyncStateRepository(session)
        assert len(await repo.get_recent_durations(tenant.id, "reconcile")) == 1
        assert await repo.get_recent_durations(tenant.id, "full") == []
        audits = await SqlAlchemyAuditLogger(session).list_recent_by_action(
            action="sync.reconcile_completed", tenant_id=tenant.id, limit=5
        )
    assert audits and audits[0].payload["drift"]["count"] == 1 and audits[0].payload["updated"] == 1


async def test_drift_is_reported_every_cycle_and_returns_to_zero_once_written(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 6})
    world = ShopifyWorld(["A", "B"], levels={"A": 0, "B": 6})
    with world:
        first = await _reconcile(session_factory, tenant)
        second = await _reconcile(session_factory, tenant)
    assert first["drift"]["count"] == 1 and second["drift"]["count"] == 0
    outcome = (await _state(session_factory, tenant.id))["last_reconcile_outcome"]
    assert outcome["drift"]["count"] == 0  # the per-cycle health number is the latest one


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


async def test_a_sku_bims_reports_as_zero_is_pushed_as_zero(migrated_db):
    """The operator's rule: BIMS is the source of truth. If BIMS says there is
    nothing, Shopify is told there is nothing."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    live = {f"L{i}": 9 for i in range(9)}  # enough real changes to stay under the zero guard
    await land_snapshot(session_factory, tenant, {**live, "SOLD_OUT": 0})
    world = ShopifyWorld(
        [*live, "SOLD_OUT"], levels={**{sku: 1 for sku in live}, "SOLD_OUT": 7}
    )
    with world:
        result = await _reconcile(session_factory, tenant)
    assert result["status"] == "ok"
    assert world.pushed["SOLD_OUT"] == 0 and world.levels["SOLD_OUT"] == 0


async def test_a_sku_bims_does_not_mention_is_never_pushed(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5}, no_stock=("NOT_MENTIONED",))
    world = ShopifyWorld(["A", "NOT_MENTIONED"], levels={"A": 1, "NOT_MENTIONED": 12})
    with world:
        await _reconcile(session_factory, tenant)
    assert world.pushed == {"A": 5} and world.levels["NOT_MENTIONED"] == 12


async def test_absent_stock_means_zero_only_when_the_tenant_opts_in(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"absent_stock_means_zero": True})
    live = {f"A{i}": 5 for i in range(10)}  # enough live SKUs to stay under the zero guard
    await land_snapshot(session_factory, tenant, live, no_stock=("GHOST",))
    world = ShopifyWorld([*live, "GHOST"], levels={**{s: 1 for s in live}, "GHOST": 3})
    with world:
        await _reconcile(session_factory, tenant)
    assert world.pushed == {**live, "GHOST": 0}


async def test_an_unknown_completeness_snapshot_is_usable(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5}, complete=None)
    world = ShopifyWorld(["A"], levels={"A": 1})
    with world:
        assert (await _reconcile(session_factory, tenant))["status"] == "ok"


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
    # Above GUARD_MIN_SAMPLE: below it the ratio is not evaluated at all, because
    # a handful of sold-out items is noise, not a catalog wipe.
    size = GUARD_MIN_SAMPLE + 5
    skus = [f"S{i}" for i in range(size)]
    await land_snapshot(session_factory, tenant, {sku: 0 for sku in skus})  # a "bad" snapshot
    world = ShopifyWorld(skus, levels={sku: 5 for sku in skus})
    with world:
        result = await _reconcile(session_factory, tenant)

    assert result["status"] == "needs_confirmation"
    assert result["reason"] == "zero_ratio_exceeded"
    assert world.write_calls == 0 and world.levels == {sku: 5 for sku in skus}
    assert result["drift"]["count"] == size  # the drift is still reported
    assert result["report"]["guard"]["fired"] is True
    state = await _state(session_factory, tenant.id)
    outcome = state["last_reconcile_outcome"]
    assert outcome["status"] == "needs_confirmation" and outcome["drift"]["count"] == size
    assert state["last_run_at"] is None  # nothing was written: the watermark did not advance


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


async def test_dry_run_reports_the_drift_without_writing_or_advancing_anything(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"A": 5})
    world = ShopifyWorld(["A"], levels={"A": 2})
    with world:
        result = await _reconcile(session_factory, tenant, dry_run=True)
    assert result["dry_run"] is True and result["drift"]["count"] == 1
    assert world.write_calls == 0 and world.levels == {"A": 2}
    assert (await _state(session_factory, tenant.id))["last_run_at"] is None


async def test_dry_run_still_reports_when_the_guard_would_have_fired(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"max_changed_variants": 1})
    await land_snapshot(session_factory, tenant, {"A": 5, "B": 5})
    world = ShopifyWorld(["A", "B"], levels={"A": 1, "B": 1})
    with world:
        result = await _reconcile(session_factory, tenant, dry_run=True)
    assert result["dry_run"] is True and result["guard_fired"] is True
    assert result["would_update"] == 2 and world.write_calls == 0


async def test_a_failed_write_is_recorded_and_does_not_advance_the_watermark(
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
    world = ShopifyWorld(["A"], levels={"A": 2}, fail_writes=True)
    with world, pytest.raises(Exception):  # noqa: B017 - any transport failure
        await _reconcile(session_factory, tenant)
    state = await _state(session_factory, tenant.id)
    outcome = state["last_reconcile_outcome"]
    assert outcome["status"] == "failed" and "error" in outcome
    assert state["last_error"].startswith("reconcile: ") and state["last_run_at"] is None


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
