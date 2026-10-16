"""Bounded concurrent stock fetch: same result as sequential, never partial."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from bims_shopify.adapters.bims import stock as stock_module
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter
from bims_shopify.ops.catalog import fetch_stock_by_sku


class FakeStockClient:
    """Serves deterministic stock per SKU; tracks in-flight calls and request ids."""

    def __init__(self, *, fail_on_batch: int | None = None, delay: float = 0.01) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.request_ids: list[str] = []
        self.calls = 0
        self._fail_on_batch = fail_on_batch
        self._delay = delay

    async def stock_fenicio(
        self, *, skus: list[str], warehouse_ids: Any, request_id: str
    ) -> dict[str, Any]:
        self.calls += 1
        self.request_ids.append(request_id)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
            if self._fail_on_batch is not None and skus[0] == f"S{self._fail_on_batch}":
                raise RuntimeError("bims exploded")
            return {
                "data": {
                    "stockPorSku": [
                        {"sku": s, "stock": (int(s[1:]) % 7) - 2} for s in skus
                    ]
                }
            }
        finally:
            self.in_flight -= 1


def _skus(n: int) -> list[str]:
    return [f"S{i}" for i in range(n)]


def _tenant() -> Any:
    return SimpleNamespace(stock_warehouse_ids=[1], slug="t")


@pytest.fixture(autouse=True)
def small_batches(monkeypatch):
    monkeypatch.setattr(stock_module, "STOCK_BATCH_SIZE", 10)
    monkeypatch.setattr("bims_shopify.ops.catalog.STOCK_BATCH_SIZE", 10, raising=False)
    monkeypatch.setattr("bims_shopify.adapters.bims.erp_adapter.STOCK_BATCH_SIZE", 10)


async def test_catalog_stock_merged_map_equals_sequential():
    skus = _skus(95) + _skus(20)  # includes duplicates
    seq = await fetch_stock_by_sku(FakeStockClient(), skus, [1], concurrency=1)
    par = await fetch_stock_by_sku(FakeStockClient(), skus, [1], concurrency=4)
    assert par == seq
    assert list(par) == list(seq)  # deterministic key order too


async def test_adapter_stock_merged_map_equals_sequential():
    skus = _skus(95)
    seq = await BIMSERPAdapter(FakeStockClient()).fetch_stock_levels(
        _tenant(), skus, concurrency=1
    )
    par = await BIMSERPAdapter(FakeStockClient()).fetch_stock_levels(
        _tenant(), skus, concurrency=4
    )
    assert par == seq
    assert list(par) == list(seq)
    assert min(par.values()) == 0.0  # negatives still clamped


async def test_catalog_stock_never_exceeds_concurrency():
    client = FakeStockClient()
    await fetch_stock_by_sku(client, _skus(200), [1], concurrency=3)
    assert 1 < client.max_in_flight <= 3


async def test_adapter_stock_never_exceeds_concurrency():
    client = FakeStockClient()
    await BIMSERPAdapter(client).fetch_stock_levels(_tenant(), _skus(200), concurrency=3)
    assert 1 < client.max_in_flight <= 3


async def test_default_concurrency_is_conservative():
    # Lowered from 4 to 2 once per-warehouse detail multiplied the passes
    # over every SKU: BIMS is the client's live billing ERP and timed out.
    assert stock_module.DEFAULT_STOCK_CONCURRENCY == 2
    client = FakeStockClient()
    await fetch_stock_by_sku(client, _skus(400), [1])
    assert 1 < client.max_in_flight <= 2


async def test_request_ids_unique_per_batch():
    client = FakeStockClient()
    await fetch_stock_by_sku(client, _skus(95), [1], concurrency=4)
    assert len(client.request_ids) == 10
    assert len(set(client.request_ids)) == 10


async def test_catalog_failing_batch_propagates_without_partial_result():
    client = FakeStockClient(fail_on_batch=20)
    with pytest.raises(RuntimeError, match="bims exploded"):
        await fetch_stock_by_sku(client, _skus(95), [1], concurrency=4)


async def test_adapter_failing_batch_propagates_without_partial_result():
    client = FakeStockClient(fail_on_batch=20)
    with pytest.raises(RuntimeError, match="bims exploded"):
        await BIMSERPAdapter(client).fetch_stock_levels(_tenant(), _skus(95), concurrency=4)


async def test_failure_stops_launching_later_batches():
    client = FakeStockClient(fail_on_batch=0)
    with pytest.raises(RuntimeError):
        await fetch_stock_by_sku(client, _skus(500), [1], concurrency=2)
    assert client.calls < 50  # did not keep hammering BIMS after the failure


async def test_adapter_records_stock_fetch_duration():
    adapter = BIMSERPAdapter(FakeStockClient())
    assert adapter.last_stock_fetch_seconds is None
    await adapter.fetch_stock_levels(_tenant(), _skus(50), concurrency=2)
    assert adapter.last_stock_fetch_seconds is not None
    assert adapter.last_stock_fetch_seconds >= 0.01
