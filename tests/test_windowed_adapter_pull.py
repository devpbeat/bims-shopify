"""End-to-end windowed catalog pull through the real client (offset-aware mock)."""
from __future__ import annotations

import httpx
import respx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter

TOTAL = 600  # 250 + 250 + 100, then the empty page; the window overshoots it


def _serve(request: httpx.Request) -> httpx.Response:
    offset = int(request.url.params["offset"])
    rows = [
        {"Product": {"id": i, "code2": f"SKU-{i}", "name": "p", "sell_price": "1"}}
        for i in range(offset, min(offset + 250, TOTAL))
    ]
    return httpx.Response(200, json={"status": "ok", "count": str(TOTAL), "data": rows})


@respx.mock
async def test_adapter_default_windowed_pull_is_complete(tenant):
    route = respx.get(f"{tenant.bims_base_url}/api/products/index.json").mock(side_effect=_serve)
    adapter = BIMSERPAdapter(BIMSClient(tenant))
    rows = await adapter._fetch_all_products(tenant, None)

    assert [r["id"] for r in rows] == list(range(TOTAL))
    pull = adapter.last_catalog_pull
    assert pull is not None
    assert pull.complete is True
    assert pull.stop_reason == "empty_page"
    assert pull.warnings == []
    assert pull.pages == 4  # 3 data pages + the first empty one
    assert pull.duration_seconds > 0
    assert route.call_count >= 4  # overshoot requests are allowed, results discarded
