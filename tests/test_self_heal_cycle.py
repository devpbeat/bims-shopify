"""The scheduled write cycle: reconcile stock, create what is missing, give the
new variants their stock, publish what qualifies.

The point of these tests is the whole loop: a size added in BIMS ends up in the
store WITH its stock and (when it passes the gates) published, with nobody
running anything by hand, and a cycle with nothing to do writes nothing.
"""
from __future__ import annotations

from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.ops.sync_workers import do_cycle
from tests.heal_world import HealShopify, land_catalog, product

JEANS = [
    ("121790", "JEANS OTTO RECTO (28)", 3.0),
    ("121791", "JEANS OTTO RECTO (29)", 2.0),
    ("121792", "JEANS OTTO RECTO (30)", 17.0),
]


async def _cycle(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await do_cycle(tenant, session, **kwargs)


async def _state(session_factory, tenant_id):
    async with session_factory() as session:
        return await SqlAlchemySyncStateRepository(session).get_status(tenant_id)


async def test_a_size_added_in_bims_reaches_the_store_with_its_stock_by_itself(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify([product(1, "JEANS OTTO RECTO", {"121790": "28", "121791": "29"})])
    with world:
        result = await _cycle(session_factory, tenant)

    assert world.product_sets == []  # the product was not re-created
    assert world.skus() == {"121790", "121791", "121792"}
    assert world.pushed == {"121790": 3, "121791": 2, "121792": 17}
    assert result["create"]["report"]["variants_added"]["variants"] == 1
    assert result["stock"]["status"] == "ok"
    assert result["stock_after_create"]["status"] == "ok"


async def test_the_new_variant_gets_its_stock_because_shopify_holds_no_level_for_it(migrated_db):
    """There is no remembered "last pushed" value to mislead the second stock
    pass: it reads what Shopify holds, and a variant created a moment ago has no
    inventory level, so it gets its stock. The two variants already right are
    not rewritten."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify([product(1, "JEANS OTTO RECTO", {"121790": "28", "121791": "29"})])
    world.levels = {"121790": 3, "121791": 2}
    with world:
        await _cycle(session_factory, tenant)

    assert world.pushed == {"121792": 17}


async def test_a_wholly_new_product_is_created_given_stock_and_published_when_it_qualifies(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"publish_require_images": False})
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        result = await _cycle(session_factory, tenant)

    assert result["create"]["report"]["created"]["products"] == 1
    assert world.pushed == {"121790": 3, "121791": 2, "121792": 17}
    assert world.find("JEANS OTTO RECTO")["status"] == "ACTIVE"
    assert result["publish"]["report"]["published"]["count"] == 1
    # Stock first, then publish: the product was never visible while it showed zero.
    assert world.count("inventory") >= 1 and world.count("publish") == 1


async def test_a_new_product_without_images_stays_a_draft_by_the_existing_gate(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        result = await _cycle(session_factory, tenant)

    assert world.find("JEANS OTTO RECTO")["status"] == "DRAFT"
    assert result["publish"]["report"]["skipped_no_images"]["count"] == 1
    assert world.publishes == []


async def test_a_cycle_with_nothing_to_do_writes_nothing_and_does_not_scan_twice(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify([product(1, "JEANS OTTO RECTO", {"121790": "28", "121791": "29", "121792": "30"})])
    with world:
        await _cycle(session_factory, tenant)  # settles: writes the stock, learns the SKUs
        world.calls.clear()

        # Every cycle re-verifies the mirror: ONE Shopify read of the catalog's quantities,
        # and since nothing differs, no write. The stages already judged this snapshot.
        same = await _cycle(session_factory, tenant)
        assert world.calls == {"inventory_read": 1}, world.calls
        assert same["create"]["status"] == "unchanged" and same["publish"]["status"] == "unchanged"

        await land_catalog(session_factory, tenant, JEANS)  # a NEW snapshot, same catalog
        world.calls.clear()
        await _cycle(session_factory, tenant)

    assert world.write_calls == 0
    assert world.count("inventory_read") == 1  # the stock pass; the create stage added no scan
    assert world.count("products_listing") == 1  # the draft listing: cheap, no publications call
    assert world.count("publications") == 0


async def test_each_stage_can_be_switched_off_per_tenant(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(auto_create_missing=False, auto_publish=False)
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify([_draft_with_stock()])
    with world:
        result = await _cycle(session_factory, tenant)

    assert result["create"]["status"] == "disabled" and result["publish"]["status"] == "disabled"
    assert world.product_sets == [] and world.bulk_creates == [] and world.publishes == []
    assert world.count("products_listing") == 0
    assert result["stock"]["status"] == "ok"  # the stock pass is not a stage: it always runs


def _draft_with_stock():
    return product(9, "DRAFTY", {"D1": None}, status="DRAFT", published=False, option="Title")


async def test_the_mass_zero_guard_stops_creating_and_publishing_on_that_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    existing = {f"E{i}": f"EXISTING {i} (M)" for i in range(10)}
    # BIMS "lost" all stock of the existing products, and shows one new product with stock.
    items = [(sku, name, 0.0) for sku, name in existing.items()] + [("N1", "NEW ONE (M)", 4.0)]
    await land_catalog(session_factory, tenant, items)
    world = HealShopify([product(i + 1, f"EXISTING {i}", {f"E{i}": "M"}) for i in range(10)])
    world.products.append(_draft_with_stock())
    world.levels = {sku: 5 for sku in existing}  # what Shopify really holds
    with world:
        result = await _cycle(session_factory, tenant)

    assert result["stock"]["status"] == "needs_confirmation"
    assert result["create"]["status"] == "declined" and result["create"]["reason"] == "stock_guard_fired"
    assert result["publish"]["status"] == "declined"
    assert world.product_sets == [] and world.publishes == [] and world.pushed == {}


async def test_absent_stock_is_never_pushed_as_zero_inside_the_cycle(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, [("A", "HAS (M)", 5.0), ("U", "UNKNOWN (M)", None)])
    world = HealShopify([product(1, "HAS", {"A": "M"}), product(2, "UNKNOWN", {"U": "M"})])
    with world:
        await _cycle(session_factory, tenant)

    assert world.pushed == {"A": 5}


async def test_a_failing_create_does_not_stop_the_stock_push_or_the_publish(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"publish_require_images": False})
    await land_catalog(session_factory, tenant, [("A", "LIVE (M)", 5.0), ("B", "BROKEN (M)", 1.0)])
    world = HealShopify([product(1, "LIVE", {"A": "M"}), _draft_ready("D1")])
    world.fail_titles = {"BROKEN"}
    with world:
        result = await _cycle(session_factory, tenant)

    assert result["stock"]["status"] == "ok" and world.pushed["A"] == 5
    assert result["create"]["report"]["failed"]["count"] == 1
    assert result["publish"]["status"] in ("ok", "noop")


def _draft_ready(sku):
    return product(8, "READY DRAFT", {sku: None}, status="DRAFT", published=False, option="Title")


async def test_a_stage_that_blows_up_is_reported_not_raised(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, [("A", "NEW (M)", 5.0)])

    async def boom(*_args, **_kwargs):
        raise RuntimeError("shopify exploded")

    monkeypatch.setattr("bims_shopify.ops.sync_workers.create_missing", boom)
    world = HealShopify()
    with world:
        result = await _cycle(session_factory, tenant)

    assert result["stock"]["status"] == "ok"
    assert result["create"]["status"] == "failed" and "exploded" in result["create"]["error"]
    outcome = (await _state(session_factory, tenant.id))["last_heal_outcome"]["create"]
    assert outcome["status"] == "failed"


async def test_the_cycle_never_calls_bims(migrated_db):
    """BIMS routes are not mocked here: a call would raise."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        await _cycle(session_factory, tenant)


async def test_every_stage_works_from_the_snapshot_the_cycle_started_with(migrated_db, monkeypatch):
    """The stock pass takes as long as a pull, so a newer snapshot can land while it runs.
    The create stage must still be judged by the guard verdict of the snapshot the cycle
    compared, never act on the newer one the guard has not seen."""
    from bims_shopify.ops import sync_workers

    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    existing = {f"E{i}": f"EXISTING {i} (M)" for i in range(10)}
    bad = [(sku, name, 0.0) for sku, name in existing.items()]  # BIMS "lost" all stock
    await land_catalog(session_factory, tenant, bad)
    world = HealShopify([product(i + 1, f"EXISTING {i}", {f"E{i}": "M"}) for i in range(10)])
    world.levels = {sku: 5 for sku in existing}

    real_reconcile = sync_workers.do_reconcile

    async def reconcile_then_a_new_snapshot_lands(*args, **kwargs):
        result = await real_reconcile(*args, **kwargs)
        # a pull finished meanwhile: a different snapshot, with a new product in stock
        await land_catalog(session_factory, tenant, [*bad, ("N1", "NEW ONE (M)", 4.0)])
        return result

    monkeypatch.setattr(sync_workers, "do_reconcile", reconcile_then_a_new_snapshot_lands)
    with world:
        result = await _cycle(session_factory, tenant)

    assert result["stock"]["status"] == "needs_confirmation"  # the guard fired on snapshot 1
    assert result["create"]["status"] == "declined"
    assert result["create"]["reason"] == "stock_guard_fired"  # not "creating from snapshot 2"
    assert world.product_sets == [] and world.pushed == {}
