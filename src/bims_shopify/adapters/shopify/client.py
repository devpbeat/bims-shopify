"""Shopify Admin GraphQL API client (API version 2025-07)."""
from __future__ import annotations

import asyncio
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
        if not quantities:
            return
        data = await self._graphql(
            _INVENTORY_SET_QUANTITIES,
            {
                "input": {
                    "reason": "correction",
                    "name": "available",
                    "quantities": quantities,
                }
            },
        )
        self._check_user_errors(data.get("inventorySetQuantities"))

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


class ShopifyGraphQLError(RuntimeError):
    pass
