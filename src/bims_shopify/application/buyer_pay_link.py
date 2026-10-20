"""Look up the card-payment link a buyer still owes, for the Thank you page."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from bims_shopify.domain.payment import PaymentIntent
from bims_shopify.domain.tenant import PaymentProvider, Tenant

PROVIDER = PaymentProvider.BANCARD_VPOS.value
OUTSTANDING_STATUSES = frozenset({"pending", "failed"})
# Checkout UI extensions report the order as `gid://shopify/OrderIdentity/<id>`,
# while the Admin API uses `gid://shopify/Order/<id>`. Both carry the same numeric
# id, so accept either form (and a bare number) rather than only the Admin one.
_ORDER_ID = re.compile(r"^(?:gid://shopify/Order(?:Identity)?/)?(\d{1,20})$")


class TenantLookupPort(Protocol):
    async def get_by_shopify_domain(self, shopify_shop_domain: str) -> Tenant | None: ...


class IntentLookupPort(Protocol):
    async def get_by_order_id(self, tenant_id: int, provider: str, order_id: str) -> PaymentIntent | None: ...


@dataclass(frozen=True)
class PayLink:
    """``pay_url`` is set only when a card payment is genuinely outstanding."""

    pay_url: str | None = None


def parse_order_id(raw: object) -> str | None:
    """The numeric Shopify order id from either a GID or a bare number."""
    match = _ORDER_ID.match(raw.strip()) if isinstance(raw, str) else None
    return match.group(1) if match else None


class FindBuyerPayLink:
    def __init__(self, tenants: TenantLookupPort, intents: IntentLookupPort) -> None:
        self._tenants = tenants
        self._intents = intents

    async def run(self, shop_domain: str, raw_order_id: object) -> PayLink:
        order_id = parse_order_id(raw_order_id)
        if order_id is None:
            return PayLink()
        tenant = await self._tenants.get_by_shopify_domain(shop_domain.strip().lower())
        if tenant is None or tenant.id is None or not tenant.active:
            return PayLink()
        if tenant.payment_provider != PaymentProvider.BANCARD_VPOS:
            return PayLink()
        intent = await self._intents.get_by_order_id(tenant.id, PROVIDER, order_id)
        if intent is None or not intent.checkout_url or intent.status not in OUTSTANDING_STATUSES:
            return PayLink()
        return PayLink(pay_url=intent.checkout_url)
