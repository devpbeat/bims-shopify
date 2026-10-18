"""Stage 1 of the scheduled cycle: create what BIMS has in stock and the store lacks.

Everything is judged from the persisted snapshot; BIMS is never called. A
product with no SKU in the store is created, a size added in BIMS to a product
already in the store is added to THAT product, and a cycle with nothing new
touches Shopify zero times.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.ops.sync_workers import do_create_missing
from tests.heal_world import HealShopify, land_catalog, product


async def _create(session_factory, tenant, **kwargs):
    async with session_factory() as session:
        return await do_create_missing(tenant, session, **kwargs)


async def _state(session_factory, tenant_id):
    async with session_factory() as session:
        return await SqlAlchemySyncStateRepository(session).get_status(tenant_id)


JEANS = [
    ("121790", "JEANS OTTO RECTO (28)", 3.0),
    ("121791", "JEANS OTTO RECTO (29)", 2.0),
    ("121792", "JEANS OTTO RECTO (30)", 17.0),
]


# -- creating ----------------------------------------------------------------------


async def test_a_wholly_new_product_is_created_as_a_draft_with_every_size(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        result = await _create(session_factory, tenant)

    assert result["status"] == "ok"
    assert len(world.product_sets) == 1 and world.bulk_creates == []
    created = world.product_sets[0]
    assert created["status"] == "DRAFT"
    assert [v["sku"] for v in created["variants"]] == ["121790", "121791", "121792"]
    assert all(v["inventoryItem"]["tracked"] is True for v in created["variants"])
    assert result["report"]["created"]["products"] == 1
    assert result["report"]["created"]["variants"] == 3


async def test_a_size_added_in_bims_is_added_to_the_existing_product_not_recreated(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify([product(1, "JEANS OTTO RECTO", {"121790": "28", "121791": "29"})])
    with world:
        result = await _create(session_factory, tenant)

    assert result["status"] == "ok"
    assert world.product_sets == []
    assert len(world.bulk_creates) == 1
    call = world.bulk_creates[0]
    assert call["productId"] == "gid://shopify/Product/1"
    assert [v["inventoryItem"]["sku"] for v in call["variants"]] == ["121792"]
    assert call["variants"][0]["inventoryItem"]["tracked"] is True
    assert call["variants"][0]["inventoryQuantities"] == [
        {"locationId": tenant.shopify_location_id, "availableQuantity": 0}
    ]
    assert len(world.products) == 1  # still one product, now with three variants
    assert result["report"]["variants_added"] == {
        "products": 1,
        "variants": 1,
        "sample": [
            {"title": "JEANS OTTO RECTO", "product_id": "gid://shopify/Product/1", "skus": ["121792"]}
        ],
    }
    assert result["report"]["created"]["products"] == 0


async def test_a_size_value_the_product_already_has_is_a_conflict_not_forced(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    # The store's size "30" sits under another SKU: adding 121792 would duplicate it.
    world = HealShopify([product(1, "JEANS OTTO RECTO", {"121790": "28", "121791": "29", "X-30": "30"})])
    with world:
        result = await _create(session_factory, tenant)

    assert world.bulk_creates == [] and world.product_sets == []
    assert result["report"]["conflicts"]["count"] == 1
    assert result["report"]["conflicts"]["sample"][0]["sku"] == "121792"
    assert "121792" not in world.skus()


async def test_only_products_with_confirmed_stock_are_created(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(
        session_factory,
        tenant,
        [
            ("S1", "WITH STOCK (M)", 4.0),
            ("Z1", "ZERO STOCK (M)", 0.0),
            ("U1", "UNKNOWN STOCK (M)", None),  # absent from the stock answer: unknown, not zero
        ],
    )
    world = HealShopify()
    with world:
        result = await _create(session_factory, tenant)

    assert [p["title"] for p in world.product_sets] == ["WITH STOCK"]
    assert result["report"]["created"]["products"] == 1


async def test_a_failing_product_does_not_stop_the_others_and_is_retried_next_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    items = [("A1", "GOOD ONE (M)", 1.0), ("B1", "BAD ONE (M)", 1.0), ("C1", "GOOD TWO (M)", 1.0)]
    await land_catalog(session_factory, tenant, items)
    world = HealShopify()
    world.fail_titles = {"BAD ONE"}
    with world:
        first = await _create(session_factory, tenant)
        assert first["report"]["created"]["products"] == 2
        assert first["report"]["failed"]["count"] == 1
        assert world.skus() == {"A1", "C1"}

        world.fail_titles = set()
        await land_catalog(session_factory, tenant, items)
        second = await _create(session_factory, tenant)

    assert second["report"]["created"]["products"] == 1
    assert world.skus() == {"A1", "B1", "C1"}


# -- the steady state is cheap ---------------------------------------------------------


async def test_nothing_to_do_writes_nothing_and_a_known_catalog_costs_no_shopify_call(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        await _create(session_factory, tenant)  # heals once
        listings, writes = world.count("variants_listing"), world.write_calls

        # The very same snapshot: nothing new, not even a database read of the rows.
        same = await _create(session_factory, tenant)
        assert same["status"] == "unchanged"

        # A NEW snapshot with the same catalog: judged from the remembered SKU index.
        await land_catalog(session_factory, tenant, JEANS)
        again = await _create(session_factory, tenant)

    assert again["status"] == "noop"
    assert world.count("variants_listing") == listings
    assert world.write_calls == writes


async def test_a_new_bims_sku_triggers_one_scan_and_only_that_creation(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        await _create(session_factory, tenant)
        listings = world.count("variants_listing")
        await land_catalog(session_factory, tenant, [*JEANS, ("999", "BRAND NEW (L)", 5.0)])
        result = await _create(session_factory, tenant)

    assert result["status"] == "ok"
    assert world.count("variants_listing") == listings + 1
    assert world.count("productSet") == 2  # the jeans once, the new product once
    assert world.skus() == {"121790", "121791", "121792", "999"}


async def test_a_stale_sku_index_is_rescanned_so_a_deleted_product_comes_back(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        await _create(session_factory, tenant)
        world.products.clear()  # somebody deleted it in the Shopify admin

        await land_catalog(session_factory, tenant, JEANS)
        untouched = await _create(session_factory, tenant)
        assert untouched["status"] == "noop" and world.products == []

        async with session_factory() as session:  # the index is now older than its limit
            repo = SqlAlchemySyncStateRepository(session)
            index = await repo.get_shopify_sku_index(tenant.id)
            index["at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
            await repo.set_shopify_sku_index(tenant.id, index)
        await land_catalog(session_factory, tenant, JEANS)
        healed = await _create(session_factory, tenant)

    assert healed["status"] == "ok"
    assert world.skus() == {"121790", "121791", "121792"}


async def test_conflicts_are_parked_so_they_do_not_rescan_every_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify([product(1, "JEANS OTTO RECTO", {"121790": "28", "121791": "29", "X-30": "30"})])
    with world:
        await _create(session_factory, tenant)
        listings = world.count("variants_listing")
        await land_catalog(session_factory, tenant, JEANS)
        again = await _create(session_factory, tenant)

    assert again["status"] == "noop"
    assert world.count("variants_listing") == listings


# -- per-tenant switch and the safety rules -------------------------------------------------


async def test_the_stage_can_be_switched_off_per_tenant(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(auto_create_missing=False)
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        result = await _create(session_factory, tenant)

    assert result["status"] == "disabled"
    assert world.total_calls == 0
    assert (await _state(session_factory, tenant.id))["last_heal_outcome"] is None


async def test_it_declines_without_a_usable_snapshot_and_never_touches_shopify(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    world = HealShopify()
    with world:
        missing = await _create(session_factory, tenant)

        await land_catalog(
            session_factory, tenant, JEANS, finished_at=datetime.now(UTC) - timedelta(hours=5)
        )
        stale = await _create(session_factory, tenant)

        await land_catalog(session_factory, tenant, JEANS, complete=False)
        incomplete = await _create(session_factory, tenant)

    assert [r["status"] for r in (missing, stale, incomplete)] == ["declined"] * 3
    assert [r["reason"] for r in (missing, stale, incomplete)] == ["missing", "stale", "incomplete"]
    assert world.total_calls == 0
    outcome = (await _state(session_factory, tenant.id))["last_heal_outcome"]["create"]
    assert outcome["status"] == "declined" and outcome["reason"] == "incomplete"


async def test_it_declines_when_the_sku_is_not_the_code2_the_import_creates(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"product_sku_field": "barcode"})
    await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        result = await _create(session_factory, tenant)

    assert result["status"] == "declined" and result["reason"] == "sku_field_not_code2"
    assert world.total_calls == 0


async def test_it_declines_when_the_push_guard_fired_on_this_very_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    meta = await land_catalog(session_factory, tenant, JEANS)
    async with session_factory() as session:
        await SqlAlchemySyncStateRepository(session).set_last_push_outcome(
            tenant.id, {"status": "needs_confirmation", "snapshot_id": meta.id, "reason": "zero_ratio_exceeded"}
        )
    world = HealShopify()
    with world:
        result = await _create(session_factory, tenant)

    assert result["status"] == "declined" and result["reason"] == "push_guard_fired"
    assert world.total_calls == 0


async def test_a_catalog_that_is_mostly_missing_is_refused_not_mass_created(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    present = {f"P{i}": None for i in range(60)}
    world = HealShopify([product(i + 1, f"HAVE {i}", {sku: None}, option="Title") for i, sku in enumerate(present)])
    items = [(f"P{i}", f"HAVE {i}", 1.0) for i in range(60)] + [
        (f"N{i}", f"NEW {i}", 1.0) for i in range(70)
    ]
    await land_catalog(session_factory, tenant, items)
    with world:
        result = await _create(session_factory, tenant)

    assert result["status"] == "needs_confirmation"
    assert result["report"]["missing_products"] == 70
    assert world.write_calls == 0
    outcome = (await _state(session_factory, tenant.id))["last_heal_outcome"]["create"]
    assert outcome["status"] == "needs_confirmation"


async def test_an_empty_store_is_bootstrapped_not_refused(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_catalog(session_factory, tenant, [(f"N{i}", f"NEW {i}", 1.0) for i in range(70)])
    world = HealShopify()
    with world:
        result = await _create(session_factory, tenant)

    assert result["status"] == "ok" and world.count("productSet") == 70


async def test_writes_per_cycle_are_capped_and_the_rest_waits_for_the_next(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant(field_mappings={"max_create_per_cycle": 2})
    items = [(f"N{i}", f"NEW {i}", 1.0) for i in range(5)]
    await land_catalog(session_factory, tenant, items)
    world = HealShopify()
    with world:
        first = await _create(session_factory, tenant)
        assert first["report"]["deferred"] == 3 and world.count("productSet") == 2
        await land_catalog(session_factory, tenant, items)
        await _create(session_factory, tenant)
        await land_catalog(session_factory, tenant, items)
        await _create(session_factory, tenant)

    assert world.skus() == {f"N{i}" for i in range(5)}


async def test_the_outcome_names_what_the_stage_did(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    meta = await land_catalog(session_factory, tenant, JEANS)
    world = HealShopify()
    with world:
        await _create(session_factory, tenant)

    create = (await _state(session_factory, tenant.id))["last_heal_outcome"]["create"]
    assert create["status"] == "ok"
    assert create["snapshot_id"] == meta.id
    assert create["created"]["products"] == 1
    assert create["variants_added"]["variants"] == 0
    assert create["conflicts"]["count"] == 0
    assert create["at"]

