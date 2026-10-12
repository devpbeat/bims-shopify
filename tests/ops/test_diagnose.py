"""Tests for the read-only ``diagnose`` ops command and its shared pipeline helpers."""
from __future__ import annotations

import httpx
import respx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.ops.catalog import (
    bims_gate_failure,
    fetch_bims_catalog_rows,
    filter_and_dedupe_rows,
    has_stock,
)

BIMS_URL = "https://bims.example.com"


# -- shared pipeline helpers -------------------------------------------------


def test_bims_gate_failure_passes_clean_row():
    raw = {"id": 1, "enabled": True, "exclude_ecommerce": False, "code2": "SKU-1"}
    assert bims_gate_failure(raw) is None


def test_bims_gate_failure_names_each_gate():
    assert bims_gate_failure({"enabled": False, "code2": "A"}) == "disabled"
    assert bims_gate_failure({"enabled": "0", "code2": "A"}) == "disabled"
    assert (
        bims_gate_failure({"enabled": True, "exclude_ecommerce": "1", "code2": "A"})
        == "excluded_from_ecommerce"
    )
    assert bims_gate_failure({"enabled": True, "code2": ""}) == "missing_code2"
    assert bims_gate_failure({"enabled": True, "code2": None}) == "missing_code2"
    assert bims_gate_failure({"enabled": True}) == "missing_code2"


def test_bims_gate_failure_defaults_match_the_pipeline():
    """Absent ``enabled`` means enabled; absent ``exclude_ecommerce`` means included."""
    assert bims_gate_failure({"code2": "A"}) is None


def test_bims_gate_failure_precedence_disabled_before_missing_code2():
    assert bims_gate_failure({"enabled": False, "code2": ""}) == "disabled"
    assert (
        bims_gate_failure({"enabled": True, "exclude_ecommerce": True, "code2": ""})
        == "excluded_from_ecommerce"
    )


def test_filter_and_dedupe_rows_agrees_with_bims_gate_failure():
    raws = [
        {"id": 1, "name": "ok", "code2": "A", "enabled": True},
        {"id": 2, "name": "off", "code2": "B", "enabled": False},
        {"id": 3, "name": "ex", "code2": "C", "enabled": True, "exclude_ecommerce": True},
        {"id": 4, "name": "nocode", "code2": "", "enabled": True},
    ]
    rows, _ = filter_and_dedupe_rows(raws)
    assert [r.bims_id for r in rows] == [
        str(r["id"]) for r in raws if bims_gate_failure(r) is None
    ]


def test_has_stock_is_strictly_greater_than_zero():
    assert has_stock({"A": 1.0}, "A") is True
    assert has_stock({"A": 0.0}, "A") is False
    assert has_stock({"A": -2.0}, "A") is False
    assert has_stock({}, "A") is False


# -- fetch_bims_catalog_rows mode --------------------------------------------


@respx.mock
async def test_fetch_bims_catalog_rows_defaults_to_simple_mode(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": []})
    )
    await fetch_bims_catalog_rows(BIMSClient(tenant), tenant.bims_company_id)
    assert route.calls[0].request.url.params["mode"] == "simple"


@respx.mock
async def test_fetch_bims_catalog_rows_accepts_explicit_mode(tenant):
    route = respx.get(f"{BIMS_URL}/api/products/index.json").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "data": [{"Product": {"id": 1, "code": "TAG-1"}}]}
        )
    )
    rows = await fetch_bims_catalog_rows(BIMSClient(tenant), tenant.bims_company_id, mode="full")
    assert route.calls[0].request.url.params["mode"] == "full"
    assert rows == [{"id": 1, "code": "TAG-1"}]
