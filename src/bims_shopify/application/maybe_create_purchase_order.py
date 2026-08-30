"""Use case: trigger a purchase order or restocking request when stock is low."""
from __future__ import annotations

from typing import Any

from bims_shopify.domain.product import ProductSnapshot
from bims_shopify.domain.tenant import ReorderStrategy, Tenant
from bims_shopify.ports.erp import ERPPort


class MaybeCreatePurchaseOrder:
    def __init__(self, erp: ERPPort) -> None:
        self._erp = erp

    async def run(
        self, tenant: Tenant, product: ProductSnapshot, product_id: int, reorder_quantity: float
    ) -> dict[str, Any] | None:
        if tenant.reorder_strategy == ReorderStrategy.NONE:
            return None
        if product.stock > tenant.reorder_threshold:
            return None

        if tenant.reorder_strategy == ReorderStrategy.PURCHASE_ORDER:
            return await self._erp.create_purchase_order(
                tenant,
                contact_id=tenant.default_customer_contact_id,
                lines=[{"product_id": product_id, "quantity": reorder_quantity, "price": product.price}],
            )
        if tenant.reorder_strategy == ReorderStrategy.RESTOCKING:
            return await self._erp.request_restocking(
                tenant, {"product_id": product_id, "quantity": reorder_quantity}
            )
        return None
