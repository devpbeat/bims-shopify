"""Payment intent entity for hosted-checkout style payment providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PaymentStatus(str, Enum):
    """Result of verifying a payment directly with the provider (never trust callback body)."""

    CONFIRMED = "confirmed"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True)
class OrderMetafield:
    """One metafield to write onto the storefront order.

    ``type`` is the storefront's own type name (for Shopify, e.g. ``number_integer``).
    ``value`` is always text, exactly as the Admin API stores it.
    """

    namespace: str
    key: str
    type: str
    value: str


@dataclass
class PaymentIntent:
    tenant_slug: str
    order_id: str
    amount: float
    currency: str
    checkout_url: str | None = None
    provider_reference: str | None = None
    status: str = "pending"
    process_id: str | None = None
    row_id: int | None = None
    """Persisted ``payment_intents`` primary key; set once the intent has been saved."""
    order_metafields: list[OrderMetafield] = field(default_factory=list)
    """Reconciliation details of a CONFIRMED payment, filled in by ``verify_payment``."""
