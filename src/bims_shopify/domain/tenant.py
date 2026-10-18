"""Tenant domain entity: holds per-merchant configuration for both BIMS and Shopify."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from bims_shopify.domain.stock_consolidation import (
    DEFAULT_STOCK_CONSOLIDATION,
    StockConsolidation,
)


class ReorderStrategy(str, Enum):
    PURCHASE_ORDER = "purchase_order"
    RESTOCKING = "restocking"
    NONE = "none"


class SyncMode(str, Enum):
    """How the scheduled inventory sync pulls the BIMS catalog.

    ``full`` compares the whole BIMS catalog every run (the default: BIMS's
    ``last_update`` filter is unreliable). ``incremental`` asks BIMS only for
    products modified since the stored watermark.

    DEPRECATED since the pull/push split: the pull worker always pulls the
    whole catalog (a partial pull cannot replace a snapshot, and stock changes
    do not touch a product's ``last_update``), so this no longer changes what
    the workers do. The setting is kept so stored values and API clients keep
    working.
    """

    FULL = "full"
    INCREMENTAL = "incremental"


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
    auto_import_products: bool = False
    auto_import_publish: bool = False
    auto_import_interval_minutes: int = 360
    auto_import_only_with_stock: bool = True
    sync_mode: SyncMode = SyncMode.FULL
    #: How per-warehouse quantities collapse into the figure published to
    #: Shopify. Defaults to summing only the positive warehouses -- see
    #: ``domain/stock_consolidation.py`` for why negatives must not subtract.
    stock_consolidation: StockConsolidation = DEFAULT_STOCK_CONSOLIDATION
    #: PULL worker cadence (BIMS -> snapshot). The name predates the split.
    sync_interval_minutes: int = 30
    #: PUSH worker cadence (snapshot -> Shopify).
    push_interval_minutes: int = 5
    #: RECONCILE worker cadence (Shopify's real quantities vs the snapshot).
    #: Far more expensive than a push (it pages the whole Shopify catalog), so
    #: it runs a few times a day, not every few minutes.
    reconcile_interval_minutes: int = 480
    #: The push worker declines to push from a snapshot older than this.
    snapshot_max_age_minutes: int = 120
    #: A pull with MORE than this fraction fewer rows than the previous snapshot is rejected
    #: (0.30: a 33,705 -> 11,355 row pull is a 66% drop and is refused; 1.0 disables it).
    pull_max_drop_ratio: float = 0.30
    #: Scheduled cycle, stage 1: create the BIMS products / variants with stock
    #: that the store lacks. Default ON; switch off per tenant without a deploy.
    auto_create_missing: bool = True
    #: Scheduled cycle, stage 3: publish the draft products that pass the
    #: ``publish`` gates (images, confirmed stock). Default ON.
    auto_publish: bool = True

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
