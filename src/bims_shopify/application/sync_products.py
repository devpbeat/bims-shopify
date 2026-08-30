"""Use case: sync product catalog (name/price) from BIMS to Shopify."""
from __future__ import annotations

from bims_shopify.domain.tenant import Tenant
from bims_shopify.ports.erp import ERPPort
from bims_shopify.ports.storefront import StorefrontPort


class SyncProductsToShopify:
    def __init__(self, erp: ERPPort, storefront: StorefrontPort) -> None:
        self._erp = erp
        self._storefront = storefront

    async def run(self, tenant: Tenant, since=None) -> int:
        products = await self._erp.list_products(tenant, since=since)
        for product in products:
            await self._storefront.upsert_product(tenant, product.sku, product.name, product.price)
        return len(products)
