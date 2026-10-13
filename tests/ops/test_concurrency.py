"""Bounded-concurrency behaviour of apply_import and apply_publish."""
from __future__ import annotations

import asyncio

from bims_shopify.adapters.shopify.client import ShopifyGraphQLError
from bims_shopify.ops.catalog import (
    DEFAULT_SHOPIFY_CONCURRENCY,
    BimsRow,
    apply_import,
    apply_publish,
    group_rows,
)


def _groups(count: int):
    rows = [BimsRow(str(i), f"Item {i:03d}", f"SKU-{i:03d}", 1000) for i in range(count)]
    return group_rows(rows).groups


def _targets(count: int):
    return [{"product_id": f"p{i:03d}", "title": f"T{i}", "skus": []} for i in range(count)]


class _TrackingImportClient:
    def __init__(self, fail_titles: frozenset[str] = frozenset()):
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls: list[str] = []
        self.fail_titles = fail_titles

    async def product_set(self, product_input):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.calls.append(product_input["title"])
        try:
            await asyncio.sleep(0.005)
            if product_input["title"] in self.fail_titles:
                raise ShopifyGraphQLError("boom")
            return {"id": f"gid://{product_input['title']}"}
        finally:
            self.in_flight -= 1


def test_default_concurrency_is_conservative():
    assert DEFAULT_SHOPIFY_CONCURRENCY == 8


async def test_import_never_exceeds_concurrency_limit():
    client = _TrackingImportClient()
    await apply_import(client, _groups(40), existing_skus=set(), status="DRAFT", concurrency=4)
    assert client.max_in_flight == 4


async def test_import_default_limit_applies():
    client = _TrackingImportClient()
    await apply_import(client, _groups(60), existing_skus=set(), status="DRAFT")
    assert client.max_in_flight == DEFAULT_SHOPIFY_CONCURRENCY


async def test_import_processes_each_item_exactly_once():
    client = _TrackingImportClient()
    result = await apply_import(client, _groups(50), existing_skus=set(), status="DRAFT", concurrency=8)
    assert sorted(client.calls) == [f"Item {i:03d}" for i in range(50)]
    assert len(result.created) == 50


async def test_import_isolates_transient_failure_and_is_deterministic():
    client = _TrackingImportClient(fail_titles=frozenset({"Item 007"}))
    result = await apply_import(client, _groups(20), existing_skus=set(), status="DRAFT", concurrency=8)
    assert [f["title"] for f in result.failed] == ["Item 007"]
    assert [c["title"] for c in result.created] == [
        f"Item {i:03d}" for i in range(20) if i != 7
    ]


async def test_import_skipped_existing_costs_no_call_and_keeps_input_order():
    client = _TrackingImportClient()
    result = await apply_import(
        client, _groups(10), existing_skus={"SKU-002", "SKU-005"}, status="DRAFT", concurrency=8
    )
    assert result.skipped_existing == ["Item 002", "Item 005"]
    assert "Item 002" not in client.calls and "Item 005" not in client.calls
    assert len(client.calls) == 8


async def test_import_progress_reports_completed_count_monotonically():
    seen: list[tuple[int, int | None, str]] = []

    def progress(done, total, message):
        seen.append((done, total, message))

    await apply_import(
        _TrackingImportClient(), _groups(100), existing_skus=set(), status="DRAFT",
        progress=progress, concurrency=8,
    )
    dones = [d for d, _, _ in seen]
    assert dones == sorted(dones) and len(set(dones)) == len(dones)
    assert dones[-1] == 100
    assert all(t == 100 for _, t, _ in seen)
    assert seen[0][2] == f"{seen[0][0]}/100 products processed"


async def test_import_unexpected_exception_propagates():
    class _Broken:
        async def product_set(self, product_input):
            raise ValueError("bug")

    try:
        await apply_import(_Broken(), _groups(3), existing_skus=set(), status="DRAFT")
    except ValueError:
        return
    raise AssertionError("ValueError should propagate")


class _TrackingPublishClient:
    def __init__(self, fail_publish: frozenset[str] = frozenset()):
        self.in_flight = 0
        self.max_in_flight = 0
        self.events: list[tuple[str, str]] = []
        self.fail_publish = fail_publish

    async def _track(self, kind: str, product_id: str):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.events.append((kind, product_id))
        try:
            await asyncio.sleep(0.005)
        finally:
            self.in_flight -= 1

    async def set_product_status(self, product_id, status):
        await self._track("status", product_id)

    async def publish_product(self, product_id, publication_ids):
        await self._track("publish", product_id)
        if product_id in self.fail_publish:
            raise ShopifyGraphQLError("boom")


async def test_publish_respects_limit_and_orders_per_product():
    client = _TrackingPublishClient()
    result = await apply_publish(client, _targets(30), publication_ids=["pub"], concurrency=4)
    assert result.processed == 30
    assert client.max_in_flight == 4
    for i in range(30):
        pid = f"p{i:03d}"
        kinds = [k for k, p in client.events if p == pid]
        assert kinds == ["status", "publish"]
    # different products genuinely interleave
    first_publish = next(i for i, (k, _) in enumerate(client.events) if k == "publish")
    assert any(k == "status" for k, _ in client.events[first_publish:])


async def test_publish_isolates_failure_and_is_deterministic():
    client = _TrackingPublishClient(fail_publish=frozenset({"p003", "p009"}))
    result = await apply_publish(client, _targets(15), publication_ids=["pub"], concurrency=8)
    assert result.processed == 13
    assert [f["product_id"] for f in result.failed] == ["p003", "p009"]


async def test_publish_progress_monotonic_to_total():
    seen: list[tuple[int, int | None, str]] = []
    await apply_publish(
        _TrackingPublishClient(), _targets(120), publication_ids=["pub"],
        progress=lambda d, t, m: seen.append((d, t, m)), concurrency=8,
    )
    dones = [d for d, _, _ in seen]
    assert dones == sorted(dones) and len(set(dones)) == len(dones)
    assert dones[-1] == 100  # multiples of 50, last one below 120
    assert seen[-1][2] == "100/120 products processed"
