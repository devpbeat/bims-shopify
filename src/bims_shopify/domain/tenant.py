"""Tenant domain entity: holds per-merchant configuration for both BIMS and Shopify."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ReorderStrategy(str, Enum):
    PURCHASE_ORDER = "purchase_order"
    RESTOCKING = "restocking"
    NONE = "none"


class PaymentProvider(str, Enum):
    PAGOPAR = "pagopar"
    BANCARD = "bancard"


@dataclass
class Tenant:
    """A single merchant configuration.

    Secrets (bims_api_key, shopify_access_token) are stored encrypted at the
    persistence layer; this domain object carries plaintext values once decrypted.
    """

    id: int | None
    slug: str
    bims_base_url: str
    bims_api_key: str
    shopify_shop_domain: str
    shopify_access_token: str
    shopify_webhook_secret: str
    shopify_location_id: str
    bims_posale_id: int
    bims_warehouse_id: int
    bims_company_id: int
    bims_currency_id: int
    bims_payment_method_id: int
    default_customer_contact_id: int
    bims_warehouse_ids: list[int] = field(default_factory=list)
    reorder_threshold: float = 0.0
    reorder_strategy: ReorderStrategy = ReorderStrategy.NONE
    bims_timezone: str = "America/Asuncion"
    payment_provider: PaymentProvider | None = None
    provider_config: dict[str, Any] = field(default_factory=dict)
    field_mappings: dict[str, Any] = field(default_factory=dict)
    active: bool = True
    push_orders_to_bims: bool = True

    @property
    def bims_auth_header(self) -> str:
        """Build the `Authorization` header value expected by BIMS.

        BIMS expects the API key sent as a plain header value (no scheme):
        ``Authorization: {tenant}_{api_key}``. The stored ``bims_api_key``
        already contains the tenant prefix.
        """
        return self.bims_api_key

    @property
    def stock_warehouse_ids(self) -> list[int]:
        """Warehouse ids to scope stock lookups to.

        Falls back to ``[bims_warehouse_id]`` when ``bims_warehouse_ids`` is
        empty, so single-warehouse tenants keep working unchanged.
        """
        return list(self.bims_warehouse_ids) if self.bims_warehouse_ids else [self.bims_warehouse_id]
