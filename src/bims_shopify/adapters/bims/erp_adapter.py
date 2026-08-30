"""ERPPort implementation backed by BIMSClient."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from bims_shopify.domain.product import ProductSnapshot
from bims_shopify.domain.sale import SaleOrder, SaleResult, SaleResultKind
from bims_shopify.domain.tenant import Tenant

from .client import BIMSClient
from .timezones import to_bims_local

PAGE_LIMIT = 250
STOCK_BATCH_SIZE = 200


def _extract_sku(product: dict[str, Any], field_mappings: dict[str, Any]) -> str | None:
    sku_field = field_mappings.get("product_sku_field", "code2")
    return product.get(sku_field)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "true", "t", "yes")
    return bool(value)


class BIMSERPAdapter:
    """Adapts BIMSClient to the ERPPort protocol used by application use cases.

    SKU/variant mapping decision: BIMS models size/color variants (e.g. "(32)",
    "(M)") as separate flat product rows, each with its own unique ``code2``.
    ``main_product_id``/``ProductsVariant`` are present in the schema but were
    empty for every sampled product on tenant mystore, so each BIMS product
    row maps 1:1 to a Shopify variant matched by SKU (``code2``); no
    parent/variant grouping is implemented.
    """

    def __init__(self, client: BIMSClient) -> None:
        self._client = client

    async def list_products(
        self, tenant: Tenant, since: datetime | None = None
    ) -> list[ProductSnapshot]:
        raw_products = await self._fetch_all_products(tenant, since)
        candidates: list[tuple[str, dict[str, Any]]] = []
        for product in raw_products:
            if not _is_truthy(product.get("enabled", True)):
                continue
            if _is_truthy(product.get("exclude_ecommerce", False)):
                continue
            company_id = product.get("company_id")
            if company_id is not None and str(company_id) != str(tenant.bims_company_id):
                continue
            sku = _extract_sku(product, tenant.field_mappings)
            if not sku:
                continue
            candidates.append((str(sku), product))

        stock_by_sku = await self.fetch_stock_levels(tenant, [sku for sku, _ in candidates])

        # Safe-by-default: a SKU absent from stock_fenicio's response has an
        # UNKNOWN stock level, not a zero one. Treating "absent" as "zero"
        # would zero out inventory for every SKU the stock endpoint didn't
        # return (observed to be ~99% of the catalog in one dry run). Only
        # push an explicit zero for absent SKUs when the tenant opts in.
        absent_means_zero = _is_truthy(tenant.field_mappings.get("absent_stock_means_zero", False))

        snapshots: list[ProductSnapshot] = []
        for sku, product in candidates:
            if sku in stock_by_sku:
                stock: float | None = stock_by_sku[sku]
            elif absent_means_zero:
                stock = 0.0
            else:
                stock = None
            snapshots.append(
                ProductSnapshot(
                    sku=sku,
                    name=product.get("name", ""),
                    price=float(product.get("sell_price") or 0),
                    stock=stock,
                    enabled=True,
                )
            )
        return snapshots

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

        products: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = await self._client.list_products(**params, offset=offset)
            items = response.get("data") or []
            if not items:
                break
            for entry in items:
                products.append(entry.get("Product", entry))
            if len(items) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
        return products

    async def fetch_stock_levels(self, tenant: Tenant, skus: list[str]) -> dict[str, float]:
        """Fetch per-SKU stock scoped to ``tenant.bims_warehouse_id``, batched."""
        result: dict[str, float] = {}
        unique_skus = list(dict.fromkeys(skus))
        for i in range(0, len(unique_skus), STOCK_BATCH_SIZE):
            batch = unique_skus[i : i + STOCK_BATCH_SIZE]
            if not batch:
                continue
            response = await self._client.stock_fenicio(
                skus=batch,
                warehouse_ids=[tenant.bims_warehouse_id],
                request_id=f"bims-shopify-{i}",
            )
            for row in (response.get("data") or {}).get("stockPorSku") or []:
                result[str(row["sku"])] = float(row.get("stock") or 0)
        return result

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
