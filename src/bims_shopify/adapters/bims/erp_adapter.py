"""ERPPort implementation backed by BIMSClient."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from bims_shopify.domain.product import ProductSnapshot
from bims_shopify.domain.sale import SaleOrder, SaleResult, SaleResultKind
from bims_shopify.domain.snapshot import CatalogPull, CatalogRow, resolve_product_snapshots
from bims_shopify.domain.stock_consolidation import consolidate_sellable_stock
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger

from .client import BIMSClient
from .paging import PagedPull, paginate_products
from .stock import STOCK_BATCH_SIZE, fetch_stock_batches
from .timezones import to_bims_local

PAGE_LIMIT = 250
logger = get_logger(__name__)


class BIMSERPAdapter:
    """Adapts BIMSClient to the ERPPort protocol used by application use cases.

    SKU/variant mapping decision: BIMS models size/color variants (e.g. "(32)",
    "(M)") as separate flat product rows, each with its own unique ``code2``.
    ``main_product_id``/``ProductsVariant`` are present in the schema but were
    empty for every sampled product on tenant mystore, so each BIMS product
    row maps 1:1 to a Shopify variant matched by SKU (``code2``); no
    parent/variant grouping is implemented.
    """

    def __init__(
        self, client: BIMSClient, *, page_concurrency: int | None = None,
        stock_concurrency: int | None = None,
    ) -> None:
        self._client = client
        self._page_concurrency = page_concurrency
        self._stock_concurrency = stock_concurrency
        #: Wall-clock seconds of the most recent ``fetch_stock_levels`` (``None``
        #: before the first one); persisted in the sync summary.
        self.last_stock_fetch_seconds: float | None = None
        #: Completeness of the most recent ``list_products`` catalog pull
        #: (``None`` before the first one). A pull that fell short of the
        #: server-reported count is also logged by the pager.
        self.last_catalog_pull: PagedPull | None = None

    async def list_products(
        self, tenant: Tenant, since: datetime | None = None
    ) -> list[ProductSnapshot]:
        """Live eligible products with their stock, straight from BIMS.

        Built on ``pull_catalog`` and the same ``resolve_product_snapshots``
        rule the snapshot reader uses (including: a SKU absent from the stock
        response has UNKNOWN stock, ``None``, never zero).
        """
        pull = await self.pull_catalog(tenant, since, warehouse_detail=False)
        return resolve_product_snapshots(pull.rows, pull.stock, tenant.field_mappings)

    async def pull_catalog(
        self, tenant: Tenant, since: datetime | None = None, *, warehouse_detail: bool = True
    ) -> CatalogPull:
        """Pull the catalog and its stock from BIMS, keeping everything needed
        to persist a snapshot.

        Every row is kept (eligible or not, so the snapshot can explain why a
        product is or is not live); stock is requested only for eligible SKUs,
        exactly as the live sync always did. The aggregate stock comes from one
        ``stock_fenicio`` pass over the tenant's warehouses together. With
        ``warehouse_detail`` and more than one warehouse, each warehouse is
        then asked on its own as well: BIMS cannot break the aggregate down, and
        that detail is what diagnoses "BIMS shows 10 in one warehouse, we show
        0". With a single warehouse the aggregate IS the per-warehouse figure,
        so no extra request is made.

        All-or-nothing: any failing request fails the pull, so a snapshot never
        mixes complete and missing stock.
        """
        raw_products = await self._fetch_all_products(tenant, since)
        paged = self.last_catalog_pull
        rows = [CatalogRow.from_raw(product, tenant.field_mappings) for product in raw_products]
        skus = [row.sku for row in rows if row.eligible and row.sku]

        stock = await self.fetch_stock_levels(tenant, skus)
        timings: dict[str, float | None] = {
            "catalog_pull_seconds": paged.duration_seconds if paged else None,
            "stock_fetch_seconds": self.last_stock_fetch_seconds,
            "warehouse_stock_seconds": None,
        }

        by_warehouse: dict[str, dict[int, float]] = {}
        warehouse_ids = tenant.stock_warehouse_ids
        if warehouse_detail:
            if len(warehouse_ids) == 1:
                for sku, quantity in stock.items():
                    by_warehouse[sku] = {warehouse_ids[0]: quantity}
            else:
                started = time.perf_counter()
                for warehouse_id in warehouse_ids:
                    per_sku, _ = await self._fetch_stock(
                        tenant,
                        skus,
                        [warehouse_id],
                        request_prefix=f"bims-shopify-wh{warehouse_id}-",
                        clamp=False,
                    )
                    for sku, quantity in per_sku.items():
                        by_warehouse.setdefault(sku, {})[warehouse_id] = quantity
                timings["warehouse_stock_seconds"] = time.perf_counter() - started

                # BIMS's own aggregate lets a negative warehouse cancel a
                # positive one, which hides merchandise the store can ship
                # (see ``consolidate_sellable_stock`` for why those negatives
                # exist). Recompute the sellable figure from the per-warehouse
                # detail we just fetched, under the tenant's rule.
                stock = {
                    sku: consolidate_sellable_stock(
                        warehouses, mode=tenant.stock_consolidation
                    )
                    for sku, warehouses in by_warehouse.items()
                }

        return CatalogPull(
            rows=rows,
            stock=stock,
            stock_by_warehouse=by_warehouse,
            reported_count=paged.reported_count if paged else None,
            complete=paged.complete if paged else None,
            pages=paged.pages if paged else 0,
            stop_reason=paged.stop_reason if paged else "empty_page",
            warnings=list(paged.warnings) if paged else [],
            timings=timings,
        )

    async def _fetch_all_products(
        self, tenant: Tenant, since: datetime | None
    ) -> list[dict[str, Any]]:
        # "simple" mode carries every field this adapter needs (code2, name,
        # sell_price, enabled, exclude_ecommerce, company_id) at roughly 3x
        # the throughput of "full" mode (which also loads pricing/variant
        # associations we don't use) — see docs/bims-api-notes.md.
        params: dict[str, Any] = {"mode": "simple", "limit": PAGE_LIMIT}
        if since is not None:
            params["last_update"] = to_bims_local(since, tenant.bims_timezone)

        async def fetch_page(offset: int) -> dict[str, Any]:
            return await self._client.list_products(**params, offset=offset)

        pull = await paginate_products(
            fetch_page, page_size=PAGE_LIMIT, concurrency=self._page_concurrency
        )
        self.last_catalog_pull = pull
        return pull.rows

    async def fetch_stock_levels(
        self, tenant: Tenant, skus: list[str], *, concurrency: int | None = None
    ) -> dict[str, float]:
        """Fetch per-SKU stock aggregated across ``tenant.stock_warehouse_ids``, batched.

        BIMS's stock_fenicio endpoint aggregates stock across the given
        warehouse_ids and can return a negative total (e.g. -1) when
        warehouse-level ledgers disagree. Negative values are clamped to 0
        here so merchants never see negative Shopify availability; the
        count of clamped SKUs is logged per run for observability.
        """
        result, seconds = await self._fetch_stock(
            tenant, skus, tenant.stock_warehouse_ids, concurrency=concurrency
        )
        self.last_stock_fetch_seconds = seconds
        return result

    async def _fetch_stock(
        self,
        tenant: Tenant,
        skus: list[str],
        warehouse_ids: list[int],
        *,
        concurrency: int | None = None,
        request_prefix: str = "bims-shopify-",
        clamp: bool = True,
    ) -> tuple[dict[str, float], float]:
        """Fetch stock for ``skus`` scoped to ``warehouse_ids``.

        ``clamp`` turns a negative quantity into 0. That is right for a figure
        headed to Shopify, but WRONG for the per-warehouse detail: a negative
        warehouse is the signal that a transfer was never registered as
        received, and clamping it to 0 hides exactly what ``diagnose`` exists
        to show. Per-warehouse pulls pass ``clamp=False``.
        """
        result: dict[str, float] = {}
        unique_skus = list(dict.fromkeys(skus))
        clamped_count = 0
        started = time.perf_counter()
        # Bounded concurrent, all-or-nothing (see ``fetch_stock_batches``);
        # responses come back in batch order so the merge matches a
        # sequential fetch.
        responses = await fetch_stock_batches(
            self._client,
            unique_skus,
            warehouse_ids,
            request_prefix=request_prefix,
            batch_size=STOCK_BATCH_SIZE,
            concurrency=concurrency if concurrency is not None else self._stock_concurrency,
        )
        seconds = time.perf_counter() - started
        for response in responses:
            for row in (response.get("data") or {}).get("stockPorSku") or []:
                stock = float(row.get("stock") or 0)
                if stock < 0 and clamp:
                    clamped_count += 1
                    stock = 0.0
                result[str(row["sku"])] = stock
        if clamped_count:
            logger.warning(
                "negative_stock_clamped",
                tenant=tenant.slug,
                clamped_count=clamped_count,
                warehouse_ids=warehouse_ids,
            )
        return result, seconds

    async def create_or_update_sale(self, tenant: Tenant, sale: SaleOrder) -> SaleResult:
        payload = _build_sale_payload(sale)
        response = await self._client.add_sale(payload, as_json=True)
        return parse_sale_response(response)

    async def verify_sale_synced(self, tenant: Tenant, sale: SaleOrder) -> dict[str, Any]:
        fields = {
            "invoice_number": sale.invoice_number or "",
            "posale_billing_code": "",
            "agency_billing_code": "",
            "stamping_code": "",
            "stamping_id": tenant.bims_posale_id,
            "billed": sale.billed,
        }
        return await self._client.verify_sale_synced(fields)

    async def create_purchase_order(
        self, tenant: Tenant, contact_id: int, lines: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload = {
            "PurchaseOrder": {"contact_id": contact_id, "status": "pending"},
            "PurchaseOrderProduct": lines,
        }
        return await self._client.create_purchase_order(payload)

    async def request_restocking(self, tenant: Tenant, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._client.request_restocking(payload)

    async def update_stock(self, tenant: Tenant, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = tenant.field_mappings.get("stock_write_endpoint", "/api/stocks/update.json")
        return await self._client.update_stock(endpoint, payload)

    async def list_deleted_product_ids(self, tenant: Tenant, since: datetime) -> list[str]:
        """GET /api/products/deleted.json?last_update=... (required filter).

        Returns the raw identifiers BIMS reports as deleted since ``since``.
        The caller is responsible for deciding what to do with them (at
        minimum: log so a human can reconcile Shopify manually; ideally
        zero-out/unpublish the matching Shopify variants).
        """
        response = await self._client.list_deleted_products(
            to_bims_local(since, tenant.bims_timezone)
        )
        data = response.get("data")
        if not data:
            return []
        if isinstance(data, list):
            return [str(entry.get("id", entry)) if isinstance(entry, dict) else str(entry) for entry in data]
        return []


def _build_sale_payload(sale: SaleOrder) -> dict[str, Any]:
    return {
        "Sale": {
            "_id": sale.external_id,
            "contact_id": sale.contact_id,
            "posale_id": sale.posale_id,
            "agency_id": sale.agency_id,
            "company_id": sale.company_id,
            "currency_id": sale.currency_id,
            "amount": sale.amount,
            "status": sale.status,
            "billed": sale.billed,
        },
        "SalesProduct": [
            {
                "product_id": line.product_id,
                "quantity": line.quantity,
                "price": line.price,
                "discount_amount": line.discount_amount,
                "currency_id": line.currency_id or sale.currency_id,
            }
            for line in sale.line_items
        ],
        "SalesPaymentMethod": [
            {
                "payment_method_id": payment.payment_method_id,
                "amount": payment.amount,
                "delayed": payment.delayed,
            }
            for payment in sale.payments
        ],
    }


def parse_sale_response(response: dict[str, Any]) -> SaleResult:
    """Normalize the oneOf-3-shapes response documented for sales/add + sales/edit."""
    if "retryable" in response:
        kind = (
            SaleResultKind.DEADLOCK_RETRYABLE
            if response.get("retryable")
            else SaleResultKind.UNCONFIRMED
        )
        return SaleResult(
            kind=kind,
            message=response.get("message"),
            retry_after_ms=response.get("retry_after_ms"),
            operation_id=response.get("operation_id"),
        )
    return SaleResult(
        kind=SaleResultKind.SUCCESS,
        data=response.get("data"),
        message=response.get("message"),
    )
