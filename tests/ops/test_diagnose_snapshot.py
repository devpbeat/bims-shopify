"""`diagnose` answers from the persisted snapshot by default.

The operator was (rightly) angry that a question whose answer sits in the
database triggered a multi-minute BIMS pull. The full live pull stays behind
an explicit `live` option.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from bims_shopify.config import Settings
from bims_shopify.ops import catalog as catalog_ops
from tests.ops.test_diagnose import _install_fakes, _raw, _sf_product
from tests.sync_world import land_snapshot

pytestmark = pytest.mark.usefixtures("sequential_paging")


def _settings_for(session_factory) -> Settings:
    url = session_factory.kw["bind"].url.render_as_string(hide_password=False)
    return Settings(
        admin_token="t", fernet_key=Fernet.generate_key().decode("utf-8"), database_url=url
    )


async def _diagnose(monkeypatch, session_factory, tenant, **options):
    monkeypatch.setattr(catalog_ops, "get_settings", lambda: _settings_for(session_factory))
    return await catalog_ops.run_diagnose(tenant, {"query": "SKU-1", **options})


async def _landed_world(monkeypatch, session_factory, make_tenant):
    tenant = await make_tenant()
    await land_snapshot(
        session_factory,
        tenant,
        {"SKU-1": 6.0},
        by_warehouse={"SKU-1": {20: 0.0, 24: 0.0}},
        unfiltered={"SKU-1": 6.0},
    )
    rec = _install_fakes(
        monkeypatch, raw_rows=[_raw(1, code2="SKU-1")], stock_by_wh={}, products=[_sf_product()]
    )
    return tenant, rec


async def test_diagnose_reads_the_snapshot_and_makes_no_bims_catalog_pull(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant, rec = await _landed_world(monkeypatch, session_factory, make_tenant)

    report = await _diagnose(monkeypatch, session_factory, tenant)

    assert rec.catalog_params == [] and rec.stock_calls == []  # BIMS was never touched
    assert report["source"] == "snapshot"
    assert report["snapshot"]["id"] > 0 and report["snapshot"]["age_minutes"] >= 0
    (row,) = report["rows"]
    assert row["bims"]["code2"] == "SKU-1"
    assert row["verdict"] == "live"  # the pushed figure (6) is stock, not the filtered 0


async def test_diagnose_output_keeps_the_per_warehouse_and_the_unfiltered_total(
    migrated_db, monkeypatch
):
    session_factory, make_tenant = migrated_db
    tenant, _ = await _landed_world(monkeypatch, session_factory, make_tenant)

    report = await _diagnose(monkeypatch, session_factory, tenant)

    (row,) = report["rows"]
    assert row["stock_by_warehouse"] == {20: 0.0, 24: 0.0}
    assert row["stock_total_all_warehouses"] == 6.0


async def test_diagnose_live_option_pulls_bims_as_before(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant, rec = await _landed_world(monkeypatch, session_factory, make_tenant)

    report = await _diagnose(monkeypatch, session_factory, tenant, live=True)

    assert report["source"] == "live"
    assert rec.catalog_params and rec.stock_calls


async def test_diagnose_without_a_snapshot_falls_back_to_live_and_says_so(migrated_db, monkeypatch):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    rec = _install_fakes(
        monkeypatch, raw_rows=[_raw(1, code2="SKU-1")], stock_by_wh={}, products=[]
    )

    report = await _diagnose(monkeypatch, session_factory, tenant)

    assert report["source"] == "live" and rec.catalog_params
    assert any("no catalog snapshot" in note for note in report["notes"])


async def test_a_snapshot_from_before_the_unfiltered_total_reports_it_as_unknown(
    migrated_db, monkeypatch
):
    session_factory, make_tenant = migrated_db
    tenant = await make_tenant()
    await land_snapshot(session_factory, tenant, {"SKU-1": 5.0})  # no unfiltered figure
    _install_fakes(monkeypatch, raw_rows=[], stock_by_wh={}, products=[_sf_product()])

    report = await _diagnose(monkeypatch, session_factory, tenant)

    (row,) = report["rows"]
    assert row["stock_total_all_warehouses"] is None  # unknown, NOT zero
    assert any("unfiltered" in note for note in report["notes"])
