"""Use case: create/reconcile a BIMS Sale from a paid Shopify order.

Idempotency: the Shopify order id is stored as `Sale._id`, a client-generated
idempotency key BIMS uses to detect duplicate submissions and replay the
original result (see SaleMutationResponse.idempotent).

Handles all three documented response shapes for /api/sales/add.json:
- success (incl. idempotent replay): return the result as-is.
- deadlock-retryable: safe to retry the same write immediately (with backoff).
- unconfirmed: write outcome is unknown; MUST call verify_synced before
  deciding whether to retry, to avoid double-booking the sale.
"""
from __future__ import annotations

import asyncio

from bims_shopify.domain.sale import SaleOrder, SaleResult, SaleResultKind
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ports.erp import ERPPort


class MaxRetriesExceededError(RuntimeError):
    pass


class ProcessShopifyOrder:
    def __init__(self, erp: ERPPort, max_retries: int = 3) -> None:
        self._erp = erp
        self._max_retries = max_retries

    async def run(self, tenant: Tenant, sale: SaleOrder) -> SaleResult:
        attempt = 0
        while True:
            attempt += 1
            result = await self._erp.create_or_update_sale(tenant, sale)

            if result.kind == SaleResultKind.SUCCESS:
                return result

            if result.kind == SaleResultKind.DEADLOCK_RETRYABLE:
                if attempt >= self._max_retries:
                    raise MaxRetriesExceededError(
                        f"Sale {sale.external_id} still deadlocking after {attempt} attempts"
                    )
                await asyncio.sleep((result.retry_after_ms or 0) / 1000)
                continue

            if result.kind == SaleResultKind.UNCONFIRMED:
                reconciliation = await self._erp.verify_sale_synced(tenant, sale)
                data = reconciliation.get("data") or {}
                if data.get("already_synced"):
                    return SaleResult(kind=SaleResultKind.SUCCESS, data=data.get("Sale"))
                if attempt >= self._max_retries:
                    raise MaxRetriesExceededError(
                        f"Sale {sale.external_id} unconfirmed after {attempt} attempts"
                    )
                continue

            raise ValueError(f"Unhandled sale result kind: {result.kind}")
