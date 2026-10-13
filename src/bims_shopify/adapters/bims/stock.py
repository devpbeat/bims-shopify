"""Bounded-concurrency batch fetch for BIMS ``stock_fenicio``.

BIMS is the client's live billing ERP (slow, flaky), so concurrency is capped
by a semaphore and the default is deliberately conservative. Callers merge the
responses themselves, always in batch order, so the merged map is identical to
a sequential fetch.
"""
from __future__ import annotations

import asyncio
from typing import Any

#: SKUs per ``stock_fenicio`` request.
STOCK_BATCH_SIZE = 200

#: Batches in flight at once. Tunable per call without a redeploy of callers.
DEFAULT_STOCK_CONCURRENCY = 4


async def fetch_stock_batches(
    client: Any,
    skus: list[str],
    warehouse_ids: list[int] | None,
    *,
    request_prefix: str,
    batch_size: int = STOCK_BATCH_SIZE,
    concurrency: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch ``skus`` in batches, at most ``concurrency`` requests in flight.

    Returns the raw responses IN BATCH ORDER (never completion order). Each
    request keeps a unique ``request_id`` (``{request_prefix}{offset}``).

    FAILURE POLICY: all-or-nothing. If any batch raises, the remaining batches
    are cancelled and the exception propagates; no partial result is ever
    returned. A half-filled stock map is indistinguishable from genuine zeroes
    to the caller and would push zero inventory into a live store. Do NOT
    "improve" this into a partial result.
    """
    limit = max(1, DEFAULT_STOCK_CONCURRENCY if concurrency is None else concurrency)
    semaphore = asyncio.Semaphore(limit)

    async def fetch(start: int) -> dict[str, Any]:
        async with semaphore:
            return await client.stock_fenicio(
                skus=skus[start : start + batch_size],
                warehouse_ids=warehouse_ids,
                request_id=f"{request_prefix}{start}",
            )

    tasks = [asyncio.create_task(fetch(i)) for i in range(0, len(skus), batch_size)]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
