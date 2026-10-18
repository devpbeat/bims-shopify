"""Stage 3 of the scheduled cycle: publish the drafts that pass the existing gates.

The gates are the manual ``publish`` command's, unchanged: at least one image
(unless the tenant opts out), confirmed stock above zero on some SKU, and every
SKU known to BIMS. The stock is the snapshot's, so the stage never calls BIMS.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.ops.sync_workers import do_publish
from tests.heal_world import PUBLICATION_ID, HealShopify, land_catalog, product


async def _publish(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await do_publish(tenant, session, **kwargs)


async def _state(session_factory, tenant_id):
    async with session_factory() as session:
        return await SqlAlchemySyncStateRepository(session).get_status(tenant_id)


def _draft(pid, title, skus, **kwargs):
    return product(pid, title, dict.fromkeys(skus), status="DRAFT", published=False, **kwargs)


async def test_a_draft_with_an_image_and_stock_is_activated_and_published(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, [("A", "JEANS (M)", 3.0)])
    world = HealShopify([_draft(1, "JEANS", ["A"])])
    with world:
        result = await _publish(session_factory, tenant)

    assert result["status"] == "ok"
    assert world.status_updates == [("gid://shopify/Product/1", "ACTIVE")]
    assert world.publishes == ["gid://shopify/Product/1"]
    assert result["report"]["published"]["count"] == 1
    assert PUBLICATION_ID  # the Online Store channel is the target


async def test_the_existing_gates_apply_unchanged(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(
        session_factory,
        tenant,
        [
            ("OK", "READY (M)", 2.0),
            ("NI", "NO IMAGE (M)", 2.0),
            ("NS", "NO STOCK (M)", 0.0),
            ("UK", "UNKNOWN (M)", None),
        ],
    )
    world = HealShopify(
        [
            _draft(1, "READY", ["OK"]),
            _draft(2, "NO IMAGE", ["NI"], media=0),
            _draft(3, "NO STOCK", ["NS"]),
            _draft(4, "UNKNOWN", ["UK"]),
        ]
    )
    with world:
        result = await _publish(session_factory, tenant)

    assert world.publishes == ["gid://shopify/Product/1"]
    report = result["report"]
    assert report["skipped_no_images"]["count"] == 1
    assert report["skipped_no_stock"]["count"] == 1
    assert report["unknown"]["count"] == 1


async def test_a_tenant_can_publish_products_without_images(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"publish_require_images": False})
    await land_catalog(session_factory, tenant, [("NI", "NO IMAGE (M)", 2.0)])
    world = HealShopify([_draft(1, "NO IMAGE", ["NI"], media=0)])
    with world:
        result = await _publish(session_factory, tenant)

    assert result["report"]["require_images"] is False
    assert world.publishes == ["gid://shopify/Product/1"]


async def test_nothing_to_publish_lists_the_drafts_once_and_writes_nothing(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, [("A", "LIVE (M)", 3.0)])
    world = HealShopify([product(1, "LIVE", {"A": "M"})])  # already ACTIVE and published
    with world:
        result = await _publish(session_factory, tenant)

    assert result["status"] == "noop"
    assert world.count("products_listing") == 1
    assert world.count("publications") == 0
    assert world.write_calls == 0


async def test_the_same_snapshot_is_not_rescanned_unless_forced(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, [("A", "LIVE (M)", 3.0)])
    world = HealShopify([product(1, "LIVE", {"A": "M"})])
    with world:
        await _publish(session_factory, tenant)
        listings = world.count("products_listing")
        again = await _publish(session_factory, tenant)
        assert again["status"] == "unchanged"
        assert world.count("products_listing") == listings

        forced = await _publish(session_factory, tenant, force=True)
    assert forced["status"] == "noop"
    assert world.count("products_listing") == listings + 1


async def test_a_new_snapshot_picks_up_an_image_added_since(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, [("A", "PIC (M)", 3.0)])
    world = HealShopify([_draft(1, "PIC", ["A"], media=0)])
    with world:
        first = await _publish(session_factory, tenant)
        assert first["status"] == "noop" and world.publishes == []
        world.find("PIC")["media"] = 2
        await land_catalog(session_factory, tenant, [("A", "PIC (M)", 3.0)])
        second = await _publish(session_factory, tenant)

    assert second["status"] == "ok" and world.publishes == ["gid://shopify/Product/1"]


async def test_the_stage_can_be_switched_off_per_tenant(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(auto_publish=False)
    await land_catalog(session_factory, tenant, [("A", "JEANS (M)", 3.0)])
    world = HealShopify([_draft(1, "JEANS", ["A"])])
    with world:
        result = await _publish(session_factory, tenant)

    assert result["status"] == "disabled"
    assert world.total_calls == 0
    assert (await _state(session_factory, tenant.id))["last_heal_outcome"] is None


async def test_it_declines_from_a_snapshot_it_cannot_trust_and_never_touches_shopify(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = HealShopify([_draft(1, "JEANS", ["A"])])
    with world:
        missing = await _publish(session_factory, tenant)
        await land_catalog(
            session_factory, tenant, [("A", "JEANS (M)", 3.0)], finished_at=datetime.now(UTC) - timedelta(hours=5)
        )
        stale = await _publish(session_factory, tenant)
        await land_catalog(session_factory, tenant, [("A", "JEANS (M)", 3.0)], complete=False)
        incomplete = await _publish(session_factory, tenant)

    assert [r["reason"] for r in (missing, stale, incomplete)] == ["missing", "stale", "incomplete"]
    assert world.total_calls == 0
    assert (await _state(session_factory, tenant.id))["last_heal_outcome"]["publish"]["reason"] == "incomplete"


async def test_it_declines_when_the_stock_guard_fired_on_this_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    meta = await land_catalog(session_factory, tenant, [("A", "JEANS (M)", 3.0)])
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_last_reconcile_outcome(
            tenant.id, {"status": "needs_confirmation", "snapshot_id": meta.id}
        )
    world = HealShopify([_draft(1, "JEANS", ["A"])])
    with world:
        result = await _publish(session_factory, tenant)

    assert result["status"] == "declined" and result["reason"] == "stock_guard_fired"
    assert world.total_calls == 0


async def test_the_outcome_names_what_the_stage_did(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    meta = await land_catalog(session_factory, tenant, [("A", "JEANS (M)", 3.0)])
    world = HealShopify([_draft(1, "JEANS", ["A"])])
    with world:
        await _publish(session_factory, tenant)

    publish = (await _state(session_factory, tenant.id))["last_heal_outcome"]["publish"]
    assert publish["status"] == "ok" and publish["snapshot_id"] == meta.id
    assert publish["published"]["count"] == 1 and publish["at"]
