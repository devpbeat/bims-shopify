"""PULL worker use case: sanity guard, atomic landing, stats.

The pull may take as long as BIMS takes, so the one thing it must never do is
replace a good snapshot with a worse one. BIMS has returned 33,705 and 11,355
rows for the same full pull hours apart; the second must not land.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from bims_shopify.adapters.persistence.models import CatalogSnapshotProductModel
from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.application.pull_snapshot import PullCatalogSnapshot
from bims_shopify.domain.snapshot import CatalogPull, CatalogRow


def make_pull(n: int, *, tag: str = "S", complete: bool | None = True) -> CatalogPull:
    rows = [
        CatalogRow(str(i), f"{tag}{i}", f"{tag}{i}", f"P{i}", 10.0, True, False, 1) for i in range(n)
    ]
    return CatalogPull(
        rows=rows,
        stock={r.sku: 1.0 for r in rows if r.sku},
        reported_count=n if complete is not None else None,
        complete=complete,
        pages=max(1, n // 250),
    )


class FakeERP:
    def __init__(self, *pulls: CatalogPull | Exception) -> None:
        self._pulls = list(pulls)
        self.calls = 0

    async def pull_catalog(self, tenant, since=None, **_kwargs):
        self.calls += 1
        item = self._pulls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def _run(session_factory, tenant, erp, stats_fn=None):
    async with session_factory() as session:
        repo = SqlAlchemySnapshotRepository(session)
        return await PullCatalogSnapshot(erp, repo, stats_fn=stats_fn).run(tenant)


async def _current_skus(session_factory, tenant_id):
    async with session_factory() as session:
        snapshot = await SqlAlchemySnapshotRepository(session).load_current(tenant_id)
    return None if snapshot is None else [r.sku for r in snapshot.rows]


async def test_first_pull_lands_even_when_small(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    result = await _run(session_factory, tenant, FakeERP(make_pull(3)))
    assert result.status == "ok"
    assert result.meta.status == "current"
    assert await _current_skus(session_factory, tenant.id) == ["S0", "S1", "S2"]


async def test_the_33705_then_11355_incident_is_rejected_and_previous_snapshot_stays(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    # Same ratio as the production incident (11,355 / 33,705 = 33.7%), scaled down.
    good, bad = make_pull(337, tag="GOOD"), make_pull(113, tag="BAD")
    assert (await _run(session_factory, tenant, FakeERP(good))).status == "ok"

    result = await _run(session_factory, tenant, FakeERP(bad))

    assert result.status == "rejected"
    assert result.reason == "too_few_rows"
    assert result.detail["rows"] == 113 and result.detail["previous_rows"] == 337
    assert result.detail["drop_ratio"] == pytest.approx(1 - 113 / 337)
    assert result.detail["max_drop_ratio"] == 0.30
    assert result.meta.status == "rejected"
    skus = await _current_skus(session_factory, tenant.id)
    assert len(skus) == 337 and skus[0] == "GOOD0"  # previous snapshot intact
    async with session_factory() as session:
        stored = (
            await session.execute(
                select(func.count()).select_from(CatalogSnapshotProductModel).where(
                    CatalogSnapshotProductModel.sku.like("BAD%")
                )
            )
        ).scalar_one()
    assert stored == 0  # the rejected pull's rows were never written


@pytest.mark.parametrize(("new_rows", "expected"), [(70, "ok"), (69, "rejected")])
async def test_threshold_boundary_is_a_30_percent_drop(migrated_db, new_rows, expected):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _run(session_factory, tenant, FakeERP(make_pull(100)))
    result = await _run(session_factory, tenant, FakeERP(make_pull(new_rows, tag="N")))
    assert result.status == expected


async def test_the_threshold_is_a_tenant_setting(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _run(session_factory, tenant, FakeERP(make_pull(100)))
    lenient = replace(tenant, pull_max_drop_ratio=0.9)
    assert (await _run(session_factory, lenient, FakeERP(make_pull(20, tag="N")))).status == "ok"
    strict = replace(tenant, pull_max_drop_ratio=0.05)
    assert (await _run(session_factory, strict, FakeERP(make_pull(18, tag="M")))).status == "rejected"


async def test_a_growing_catalog_is_never_rejected(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _run(session_factory, tenant, FakeERP(make_pull(10)))
    assert (await _run(session_factory, tenant, FakeERP(make_pull(500, tag="N")))).status == "ok"


async def test_explicitly_incomplete_pull_is_rejected_whatever_its_size(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _run(session_factory, tenant, FakeERP(make_pull(10)))
    incomplete = make_pull(12, tag="N", complete=False)  # bigger, but BIMS said more existed
    result = await _run(session_factory, tenant, FakeERP(incomplete))
    assert result.status == "rejected" and result.reason == "incomplete"
    assert result.detail["reported_count"] == 12
    assert (await _current_skus(session_factory, tenant.id))[0] == "S0"


async def test_incomplete_first_pull_is_rejected_too(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    result = await _run(session_factory, tenant, FakeERP(make_pull(5, complete=False)))
    assert result.status == "rejected"
    assert await _current_skus(session_factory, tenant.id) is None


async def test_unknown_completeness_is_not_a_rejection(migrated_db):
    """BIMS sent no count: completeness cannot be judged (None), and the size guard decides."""
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    result = await _run(session_factory, tenant, FakeERP(make_pull(5, complete=None)))
    assert result.status == "ok"
    assert result.meta.complete is None


async def test_stock_coverage_collapse_is_rejected_like_a_row_collapse(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _run(session_factory, tenant, FakeERP(make_pull(100)))
    thin = make_pull(100, tag="S")
    thin.stock = dict(list(thin.stock.items())[:20])  # stock endpoint answered for 20 of 100
    result = await _run(session_factory, tenant, FakeERP(thin))
    assert result.status == "rejected" and result.reason == "too_few_stock_rows"


async def test_failure_while_writing_leaves_the_previous_snapshot_visible(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _run(session_factory, tenant, FakeERP(make_pull(4, tag="OLD")))

    async def explode(self, snapshot_id, entries):
        raise RuntimeError("disk full")

    monkeypatch.setattr(SqlAlchemySnapshotRepository, "_insert_stock", explode)
    with pytest.raises(RuntimeError, match="disk full"):
        await _run(session_factory, tenant, FakeERP(make_pull(5, tag="NEW")))
    monkeypatch.undo()

    assert await _current_skus(session_factory, tenant.id) == ["OLD0", "OLD1", "OLD2", "OLD3"]


async def test_bims_failure_propagates_and_leaves_the_previous_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _run(session_factory, tenant, FakeERP(make_pull(4, tag="OLD")))
    with pytest.raises(RuntimeError, match="bims down"):
        await _run(session_factory, tenant, FakeERP(RuntimeError("bims down")))
    assert len(await _current_skus(session_factory, tenant.id)) == 4


async def test_stats_are_computed_by_the_pull_and_stored_with_the_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    seen = {}

    async def stats_fn(t, pull):
        seen["rows"] = len(pull.rows)
        return {"bims": {"eligible_products": 3}}

    result = await _run(session_factory, tenant, FakeERP(make_pull(3)), stats_fn=stats_fn)

    assert seen["rows"] == 3
    assert result.stats == {"bims": {"eligible_products": 3}}
    async with session_factory() as session:
        meta = await SqlAlchemySnapshotRepository(session).get_current_meta(tenant.id)
    assert meta.stats == {"bims": {"eligible_products": 3}}


async def test_stats_failure_does_not_block_the_snapshot(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()

    async def stats_fn(t, pull):
        raise RuntimeError("shopify down")

    result = await _run(session_factory, tenant, FakeERP(make_pull(3)), stats_fn=stats_fn)

    assert result.status == "ok"
    assert result.stats is None
    assert "shopify down" in result.stats_error
    assert await _current_skus(session_factory, tenant.id) == ["S0", "S1", "S2"]


async def test_stats_are_not_computed_for_a_rejected_pull(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await _run(session_factory, tenant, FakeERP(make_pull(100)))
    calls = []

    async def stats_fn(t, pull):
        calls.append(1)
        return {}

    await _run(session_factory, tenant, FakeERP(make_pull(1, tag="N")), stats_fn=stats_fn)
    assert calls == []


async def test_meta_records_started_finished_and_phase_timings(migrated_db):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    before = datetime.now(UTC)
    pull = make_pull(3)
    pull.timings = {"catalog_pull_seconds": 1.0, "stock_fetch_seconds": 2.0}
    result = await _run(session_factory, tenant, FakeERP(pull))
    meta = result.meta
    assert before <= meta.started_at <= meta.finished_at
    assert meta.timings["catalog_pull_seconds"] == 1.0
    assert meta.timings["write_seconds"] >= 0
    assert meta.timings["total_seconds"] >= 0
