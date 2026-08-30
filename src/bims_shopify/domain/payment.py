"""Payment intent entity for hosted-checkout style payment providers."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PaymentStatus(str, Enum):
    """Result of verifying a payment directly with the provider (never trust callback body)."""

    CONFIRMED = "confirmed"
    PENDING = "pending"
    FAILED = "failed"


@dataclass
class PaymentIntent:
    tenant_slug: str
    order_id: str
    amount: float
    currency: str
    checkout_url: str | None = None
    provider_reference: str | None = None
    status: str = "pending"
