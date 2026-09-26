"""Shopify Admin GraphQL API client (API version 2025-07)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from bims_shopify.domain.tenant import Tenant

API_VERSION = "2025-07"

_MAX_RETRY_ATTEMPTS = 5
_RETRY_BASE_DELAY_SECONDS = 0.5
_RETRY_MAX_DELAY_SECONDS = 8.0

_FIND_VARIANT_BY_SKU = """
query FindVariantBySku($query: String!) {
  productVariants(first: 1, query: $query) {
    nodes {
      id
      sku
      inventoryItem { id }
      product { id title }
    }
  }
}
"""

_INVENTORY_SET_QUANTITIES = """
mutation InventorySetQuantities($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt }
    userErrors { field message code }
  }
}
"""

_INVENTORY_ACTIVATE = """
mutation InventoryActivate($inventoryItemId: ID!, $locationId: ID!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) {
    inventoryLevel { id }
    userErrors { field message }
  }
}
"""

_ORDER_MARK_AS_PAID = """
mutation OrderMarkAsPaid($input: OrderMarkAsPaidInput!) {
  orderMarkAsPaid(input: $input) {
    order { id displayFinancialStatus }
    userErrors { field message }
  }
}
"""

_LIST_ALL_VARIANTS = """
query ListAllVariants($cursor: String) {
  productVariants(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      sku
      product { id title }
      inventoryItem { id tracked }
    }
  }
}
"""

_VARIANTS_BULK_UPDATE = """
mutation ProductVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku }
    userErrors { field message }
  }
}
"""

_VARIANTS_BULK_DELETE = """
mutation ProductVariantsBulkDelete($productId: ID!, $variantsIds: [ID!]!) {
  productVariantsBulkDelete(productId: $productId, variantsIds: $variantsIds) {
    product { id }
    userErrors { field message }
  }
}
"""

_PRODUCTS_COUNT = """
query ProductsCount {
  productsCount { count }
}
"""

_LIST_ALL_PRODUCTS = """
query ListAllProducts($cursor: String) {
  products(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id title }
  }
}
"""

_LIST_ALL_PRODUCTS_WITH_SKUS = """
query ListAllProductsWithSkus($cursor: String) {
  products(first: 100, after: $cursor, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      status
      variants(first: 100) {
        nodes { sku }
      }
    }
  }
}
"""

_PRODUCT_DELETE = """
mutation ProductDelete($input: ProductDeleteInput!) {
  productDelete(input: $input, synchronous: true) {
    deletedProductId
    userErrors { field message }
  }
}
"""

_PRODUCT_SET = """
mutation ProductSet($input: ProductSetInput!) {
  productSet(input: $input, synchronous: true) {
    product { id title }
    userErrors { field message code }
  }
}
"""

_PRODUCT_UPDATE_STATUS = """
mutation ProductUpdateStatus($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id status }
    userErrors { field message }
  }
}
"""

_GET_ORDER = """
query GetOrder($id: ID!) {
  order(id: $id) {
    id
    name
    displayFinancialStatus
    totalPriceSet { shopMoney { amount currencyCode } }
    lineItems(first: 100) {
      nodes { sku quantity variant { id price } }
    }
  }
}
"""


class ShopifyClient:
    def __init__(
        self,
        tenant: Tenant,
        http_client: httpx.AsyncClient | None = None,
        retry_base_delay: float = _RETRY_BASE_DELAY_SECONDS,
    ) -> None:
        self._tenant = tenant
        url = f"https://{tenant.shopify_shop_domain}/admin/api/{API_VERSION}/graphql.json"
        self._client = http_client or httpx.AsyncClient(
            base_url=url,
            headers={
                "X-Shopify-Access-Token": tenant.shopify_access_token,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self._retry_base_delay = retry_base_delay

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _is_throttled(payload: dict[str, Any]) -> bool:
        """True if a GraphQL error body reports Shopify's cost-throttling error.

        Shopify returns this as HTTP 200 with an `errors[].extensions.code`
        of `THROTTLED`, so it has to be detected from the body, not the
        status code.
        """
        for error in payload.get("errors") or []:
            if isinstance(error, dict) and (error.get("extensions") or {}).get("code") == "THROTTLED":
                return True
        return False

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a GraphQL request with bounded exponential backoff.

        Retries on HTTP 429/5xx and on Shopify's THROTTLED GraphQL error
        (which comes back as HTTP 200), honoring a `Retry-After` header when
        present. After exhausting retries it raises the same
        ShopifyGraphQLError used for any other GraphQL failure.
        """
        attempt = 0
        while True:
            attempt += 1
            response = await self._client.post("", json={"query": query, "variables": variables})

            retryable_status = response.status_code == 429 or response.status_code >= 500
            payload: dict[str, Any] | None = None
            throttled = False
            if not retryable_status:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ShopifyGraphQLError(str(exc)) from exc
                payload = response.json()
                throttled = self._is_throttled(payload)

            if retryable_status or throttled:
                if attempt >= _MAX_RETRY_ATTEMPTS:
                    if payload is not None and payload.get("errors"):
                        raise ShopifyGraphQLError(str(payload["errors"]))
                    raise ShopifyGraphQLError(
                        f"Shopify GraphQL request failed after {attempt} attempts "
                        f"(status {response.status_code})"
                    )
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    wait_seconds = float(retry_after)
                else:
                    wait_seconds = min(
                        self._retry_base_delay * (2 ** (attempt - 1)), _RETRY_MAX_DELAY_SECONDS
                    )
                await asyncio.sleep(wait_seconds)
                continue

            assert payload is not None
            if payload.get("errors"):
                raise ShopifyGraphQLError(str(payload["errors"]))
            return payload["data"]

    @staticmethod
    def _check_user_errors(mutation_result: dict[str, Any] | None) -> None:
        """Raise if the mutation payload carries a non-empty userErrors array.

        GraphQL-level `errors` (checked in `_graphql`) do not cover business
        validation failures returned by Shopify mutations (e.g. an invalid
        locationId or inventory item) — those come back as HTTP 200 with a
        populated `userErrors` array that must be checked explicitly.
        """
        user_errors = (mutation_result or {}).get("userErrors") or []
        if user_errors:
            raise ShopifyGraphQLError(str(user_errors))

    async def find_variant_by_sku(self, tenant: Tenant, sku: str) -> dict[str, Any] | None:
        data = await self._graphql(_FIND_VARIANT_BY_SKU, {"query": f"sku:{sku}"})
        nodes = data["productVariants"]["nodes"]
        return nodes[0] if nodes else None

    async def set_inventory_quantities(self, tenant: Tenant, deltas: list) -> None:
        quantities = []
        for delta in deltas:
            if not delta.variant_inventory_item_id:
                continue
            quantities.append(
                {
                    "inventoryItemId": delta.variant_inventory_item_id,
                    "locationId": tenant.shopify_location_id,
                    "quantity": int(delta.new_stock),
                }
            )
        # BIMS can carry several rows with the same code2 (duplicate SKUs),
        # which resolve to the same Shopify inventory item. Shopify rejects a
        # batch with a repeated (inventoryItemId, locationId) pair, so keep one
        # quantity per inventory item (values match — stock is by code2).
        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for q in quantities:
            deduped[(q["inventoryItemId"], q["locationId"])] = q
        quantities = list(deduped.values())
        if not quantities:
            return
        await self._set_inventory_quantities_self_healing(quantities)

    async def _set_inventory_quantities_self_healing(
        self, quantities: list[dict[str, Any]], *, retried: bool = False
    ) -> None:
        """Set inventory quantities, self-healing items that aren't activated yet.

        Legacy/pre-fix variants (created before tracking + activation was
        wired into import) come back with a per-quantity ``userErrors`` entry
        of code ``ITEM_NOT_STOCKED_AT_LOCATION`` (see
        https://shopify.dev/docs/api/admin-graphql/2025-07/mutations/inventorySetQuantities
        and https://shopify.dev/docs/api/admin-graphql/2025-07/enums/InventorySetQuantitiesUserErrorCode).
        On that error we activate the offending item(s) via ``inventoryActivate``
        (https://shopify.dev/docs/api/admin-graphql/2025-07/mutations/inventoryActivate)
        and retry the whole batch exactly once, so the regular sync gradually
        heals legacy variants instead of silently dropping their stock.
        """
        data = await self._graphql(
            _INVENTORY_SET_QUANTITIES,
            {
                "input": {
                    "reason": "correction",
                    "name": "available",
                    # Shopify 2025-07 requires either a per-item compareQuantity
                    # or this flag; we push BIMS as source of truth, so ignore.
                    "ignoreCompareQuantity": True,
                    "quantities": quantities,
                }
            },
        )
        result = data.get("inventorySetQuantities")
        user_errors = (result or {}).get("userErrors") or []
        not_stocked = [e for e in user_errors if e.get("code") == "ITEM_NOT_STOCKED_AT_LOCATION"]
        if not_stocked and not retried:
            for error in not_stocked:
                index = self._extract_quantity_index(error.get("field"))
                if index is None or index >= len(quantities):
                    continue
                quantity = quantities[index]
                await self.activate_inventory_item(
                    quantity["inventoryItemId"], quantity["locationId"]
                )
            await self._set_inventory_quantities_self_healing(quantities, retried=True)
            return
        self._check_user_errors(result)

    @staticmethod
    def _extract_quantity_index(field: Any) -> int | None:
        """Pull the list index out of a userErrors ``field`` path, e.g. ``["input", "quantities", "0", "inventoryItemId"]``."""
        for part in field or []:
            if isinstance(part, int):
                return part
            if isinstance(part, str) and part.isdigit():
                return int(part)
        return None

    async def activate_inventory_item(self, inventory_item_id: str, location_id: str) -> None:
        """Activate an inventory item at a location via ``inventoryActivate``."""
        data = await self._graphql(
            _INVENTORY_ACTIVATE,
            {"inventoryItemId": inventory_item_id, "locationId": location_id},
        )
        self._check_user_errors(data.get("inventoryActivate"))

    async def upsert_product(self, tenant: Tenant, sku: str, name: str, price: float) -> None:
        # Product creation/update requires the productSet mutation; kept minimal
        # here since inventory sync is the primary supported flow.
        existing = await self.find_variant_by_sku(tenant, sku)
        if existing is None:
            return

    async def mark_order_as_paid(self, tenant: Tenant, order_id: str) -> None:
        data = await self._graphql(_ORDER_MARK_AS_PAID, {"input": {"id": order_id}})
        self._check_user_errors(data.get("orderMarkAsPaid"))

    async def get_order(self, tenant: Tenant, order_id: str) -> dict[str, Any] | None:
        data = await self._graphql(_GET_ORDER, {"id": order_id})
        return data.get("order")

    async def build_sku_inventory_map(self, tenant: Tenant) -> dict[str, str]:
        """Return {sku: inventory_item_id} for every variant in the shop.

        Built via ``iter_all_variants`` (a full catalog listing), which —
        unlike the search-index-backed ``find_variant_by_sku`` query —
        reliably includes DRAFT products. Used by inventory sync to resolve
        every delta's variant in a single pass instead of one Shopify
        search request per SKU.
        """
        sku_map: dict[str, str] = {}
        async for node in self.iter_all_variants():
            sku = (node.get("sku") or "").strip()
            if not sku:
                continue
            inventory_item_id = (node.get("inventoryItem") or {}).get("id")
            if inventory_item_id:
                sku_map[sku] = inventory_item_id
        return sku_map

    async def iter_all_variants(self) -> AsyncIterator[dict[str, Any]]:
        """Page through every product variant in the shop.

        Yields raw GraphQL nodes shaped ``{id, sku, product: {id, title}}``.
        Used by ops scripts that need a full-catalog scan (e.g. rekey_skus).
        """
        cursor: str | None = None
        while True:
            data = await self._graphql(_LIST_ALL_VARIANTS, {"cursor": cursor})
            connection = data["productVariants"]
            for node in connection["nodes"]:
                yield node
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                return
            cursor = page_info["endCursor"]

    async def bulk_update_variants(self, product_id: str, variants: list[dict[str, Any]]) -> None:
        """Update multiple variants of a single product via productVariantsBulkUpdate."""
        data = await self._graphql(
            _VARIANTS_BULK_UPDATE, {"productId": product_id, "variants": variants}
        )
        self._check_user_errors(data.get("productVariantsBulkUpdate"))

    async def bulk_delete_variants(self, product_id: str, variant_ids: list[str]) -> None:
        """Delete multiple variants of a single product via productVariantsBulkDelete."""
        data = await self._graphql(
            _VARIANTS_BULK_DELETE, {"productId": product_id, "variantsIds": variant_ids}
        )
        self._check_user_errors(data.get("productVariantsBulkDelete"))

    async def set_product_status(self, product_id: str, status: str) -> None:
        """Set a product's status (e.g. 'DRAFT', 'ACTIVE') via productUpdate."""
        data = await self._graphql(
            _PRODUCT_UPDATE_STATUS, {"input": {"id": product_id, "status": status}}
        )
        self._check_user_errors(data.get("productUpdate"))

    async def products_count(self) -> int:
        """Return the total number of products in the shop via productsCount."""
        data = await self._graphql(_PRODUCTS_COUNT, {})
        return int(data["productsCount"]["count"])

    async def iter_all_products(self) -> AsyncIterator[dict[str, Any]]:
        """Page through every product in the shop, yielding ``{id, title}`` nodes."""
        cursor: str | None = None
        while True:
            data = await self._graphql(_LIST_ALL_PRODUCTS, {"cursor": cursor})
            connection = data["products"]
            for node in connection["nodes"]:
                yield node
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                return
            cursor = page_info["endCursor"]

    async def iter_all_products_with_skus(self) -> AsyncIterator[dict[str, Any]]:
        """Page through every product, sorted by id ascending, with its variant SKUs.

        Yields ``{id, title, skus}`` where ``skus`` is the list of non-blank
        variant SKUs (order preserved, not deduped). Used by the ``dedupe``
        ops command, which needs a stable full-catalog scan to detect
        duplicate products created by non-idempotent import runs.
        """
        cursor: str | None = None
        while True:
            data = await self._graphql(_LIST_ALL_PRODUCTS_WITH_SKUS, {"cursor": cursor})
            connection = data["products"]
            for node in connection["nodes"]:
                skus = [
                    (v.get("sku") or "").strip()
                    for v in (node.get("variants") or {}).get("nodes") or []
                ]
                yield {
                    "id": node["id"],
                    "title": node.get("title"),
                    "status": node.get("status"),
                    "skus": [s for s in skus if s],
                }
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                return
            cursor = page_info["endCursor"]

    async def delete_product(self, product_id: str) -> None:
        """Delete a single product (and all its variants) via productDelete."""
        data = await self._graphql(_PRODUCT_DELETE, {"input": {"id": product_id}})
        self._check_user_errors(data.get("productDelete"))

    async def product_set(self, product_input: dict[str, Any]) -> dict[str, Any] | None:
        """Create or update a product (with options + variants in one call) via productSet."""
        data = await self._graphql(_PRODUCT_SET, {"input": product_input})
        result = data.get("productSet")
        self._check_user_errors(result)
        return (result or {}).get("product")


class ShopifyGraphQLError(RuntimeError):
    pass
