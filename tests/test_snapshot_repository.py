"""Persisted catalog snapshot: atomic visibility, retention and metadata.

The pull worker writes a snapshot that the push worker reads while the next
pull may already be writing. A reader must only ever see a whole snapshot.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from bims_shopify.adapters.persistence.models import (
    CatalogSnapshotModel,
    CatalogSnapshotProductModel,
    CatalogSnapshotStockModel,
)
from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.domain.snapshot import CatalogPull, CatalogRow


def _pull(n: int = 3, *, complete: bool | None = True, tag: str = "A") -> CatalogPull:
    rows = [
        CatalogRow(
            bims_id=str(i),
            sku=f"{tag}{i}",
            code2=f"{tag}{i}",
            name=f"Product {tag}{i}",
            sell_price=1000.0 + i,
            enabled=True,
            exclude_ecommerce=False,
            company_id=7,
        )
        for i in range(n)
    ]
    return CatalogPull(
        rows=rows,
        stock={f"{tag}{i}": float(i) for i in range(n)},
        stock_by_warehouse={f"{tag}{i}": {1: float(i), 2: 0.0} for i in range(n)},
        reported_count=n,
        complete=complete,
        pages=1,
        stop_reason="empty_page",
        warnings=["w1"],
        timings={"catalog_pull_seconds": 1.5, "stock_fetch_seconds": 2.5},
    )


async def _land(repo, tenant_id: int, pull: CatalogPull):
    started = datetime.now(UTC)
    meta = await repo.stage(tenant_id, pull, started)
    return await repo.promote(meta.id, finished_at=datetime.now(UTC))


async def test_round_trip_keeps_rows_stock_per_warehouse_and_metadata(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        await _land(repo, tenant.id, _pull(3))
        snapshot = await repo.load_current(tenant.id)

    assert snapshot is not None
    assert [r.sku for r in snapshot.rows] == ["A0", "A1", "A2"]
    row = snapshot.rows[1]
    assert (row.bims_id, row.code2, row.name, row.sell_price) == ("1", "A1", "Product A1", 1001.0)
    assert (row.enabled, row.exclude_ecommerce, row.company_id) == (True, False, 7)
    assert snapshot.stock["A2"].quantity == 2.0
    assert snapshot.stock["A2"].by_warehouse == {1: 2.0, 2: 0.0}
    meta = snapshot.meta
    assert meta.status == "current"
    assert (meta.rows_pulled, meta.stock_rows, meta.pages) == (3, 3, 1)
    assert (meta.reported_count, meta.complete, meta.stop_reason) == (3, True, "empty_page")
    assert meta.warnings == ["w1"]
    assert meta.timings["catalog_pull_seconds"] == 1.5
    assert meta.finished_at is not None


async def test_staged_but_unpromoted_snapshot_is_invisible(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        await repo.stage(tenant.id, _pull(3), datetime.now(UTC))
        assert await repo.load_current(tenant.id) is None
        assert await repo.get_current_meta(tenant.id) is None


async def test_failed_write_leaves_previous_snapshot_and_cleans_up(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session, chunk_size=2)
        await _land(repo, tenant.id, _pull(3, tag="OLD"))

        calls = {"n": 0}
        real = repo._insert_stock

        async def exploding(snapshot_id, entries):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db went away mid-write")
            await real(snapshot_id, entries)

        monkeypatch.setattr(repo, "_insert_stock", exploding)
        with pytest.raises(RuntimeError):
            await repo.stage(tenant.id, _pull(5, tag="NEW"), datetime.now(UTC))
        monkeypatch.undo()

        snapshot = await repo.load_current(tenant.id)
        assert [r.sku for r in snapshot.rows] == ["OLD0", "OLD1", "OLD2"]
        # Nothing of the failed generation survives: its rows are gone and its
        # metadata says failed.
        leftover = (
            await session.execute(
                select(func.count()).select_from(CatalogSnapshotProductModel).where(
                    CatalogSnapshotProductModel.sku.like("NEW%")
                )
            )
        ).scalar_one()
        assert leftover == 0
        statuses = (
            await session.execute(select(CatalogSnapshotModel.status).order_by(CatalogSnapshotModel.id))
        ).scalars().all()
        assert statuses == ["current", "failed"]


async def test_killed_mid_write_orphan_is_never_visible_and_is_cleaned_next_pull(migrated_db):
    """A container restart mid-write leaves a 'building' generation behind."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        await _land(repo, tenant.id, _pull(2, tag="OLD"))
        await repo.stage(tenant.id, _pull(4, tag="DEAD"), datetime.now(UTC))  # never promoted

    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        assert [r.sku for r in (await repo.load_current(tenant.id)).rows] == ["OLD0", "OLD1"]
        await _land(repo, tenant.id, _pull(2, tag="NEW"))
        assert [r.sku for r in (await repo.load_current(tenant.id)).rows] == ["NEW0", "NEW1"]
        dead = (
            await session.execute(
                select(func.count()).select_from(CatalogSnapshotProductModel).where(
                    CatalogSnapshotProductModel.sku.like("DEAD%")
                )
            )
        ).scalar_one()
        assert dead == 0


async def test_promote_leaves_exactly_one_current_and_keeps_the_previous_for_readers(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        first = await _land(repo, tenant.id, _pull(2, tag="A"))
        second = await _land(repo, tenant.id, _pull(2, tag="B"))
        third = await _land(repo, tenant.id, _pull(2, tag="C"))

        current = (
            await session.execute(
                select(CatalogSnapshotModel.id).where(
                    CatalogSnapshotModel.tenant_id == tenant.id, CatalogSnapshotModel.is_current.is_(True)
                )
            )
        ).scalars().all()
        assert current == [third.id]

        def rows_of(model, snapshot_id):
            return select(func.count()).select_from(model).where(model.snapshot_id == snapshot_id)

        # current + previous keep their rows; older generations are purged.
        for snap, expected in ((third, 2), (second, 2), (first, 0)):
            assert (await session.execute(rows_of(CatalogSnapshotProductModel, snap.id))).scalar_one() == expected
            assert (await session.execute(rows_of(CatalogSnapshotStockModel, snap.id))).scalar_one() == expected
        # ...but the metadata history of all of them stays readable.
        history = await repo.list_recent_meta(tenant.id)
        assert [m.id for m in history] == [third.id, second.id, first.id]
        assert [m.status for m in history] == ["current", "superseded", "superseded"]


async def test_rejected_pull_is_recorded_without_touching_the_current_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        good = await _land(repo, tenant.id, _pull(3))
        now = datetime.now(UTC)
        rejected = await repo.record_rejected(
            tenant.id,
            _pull(1),
            started_at=now - timedelta(seconds=5),
            finished_at=now,
            rejection={"reason": "too_few_rows", "previous_rows": 3, "ratio": 0.33},
        )
        assert rejected.status == "rejected"
        assert rejected.rejection["reason"] == "too_few_rows"
        assert (await repo.get_current_meta(tenant.id)).id == good.id
        assert len((await repo.load_current(tenant.id)).rows) == 3
        # a rejected pull stores no rows
        count = (
            await session.execute(
                select(func.count()).select_from(CatalogSnapshotProductModel).where(
                    CatalogSnapshotProductModel.snapshot_id == rejected.id
                )
            )
        ).scalar_one()
        assert count == 0


async def test_snapshots_are_isolated_per_tenant(migrated_db):
    session_factory, make_tenant = migrated_db
    a = await make_tenant(slug="a", shopify_shop_domain="a.myshopify.com")
    b = await make_tenant(slug="b", shopify_shop_domain="b.myshopify.com")
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        await _land(repo, a.id, _pull(2, tag="A"))
        await _land(repo, b.id, _pull(1, tag="B"))
        assert [r.sku for r in (await repo.load_current(a.id)).rows] == ["A0", "A1"]
        assert [r.sku for r in (await repo.load_current(b.id)).rows] == ["B0"]
