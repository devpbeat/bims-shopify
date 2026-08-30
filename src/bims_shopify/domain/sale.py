"""Sale order and line-item entities shared between the Shopify order and BIMS sale."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SaleResultKind(str, Enum):
    """The three possible outcomes documented for /api/sales/add.json and /edit.json."""

    SUCCESS = "success"
    DEADLOCK_RETRYABLE = "deadlock_retryable"
    UNCONFIRMED = "unconfirmed"


@dataclass
class SaleLineItem:
    product_id: int
    quantity: float
    price: float
    discount_amount: float = 0.0
    currency_id: int | None = None


@dataclass
class SalePayment:
    payment_method_id: int
    amount: float
    delayed: bool = False


@dataclass
class SaleOrder:
    """A sale to be pushed to BIMS, derived from a Shopify order."""

    external_id: str
    contact_id: int
    posale_id: int
    agency_id: int
    company_id: int
    currency_id: int
    amount: float
    line_items: list[SaleLineItem] = field(default_factory=list)
    payments: list[SalePayment] = field(default_factory=list)
    status: str = "approved"
    billed: bool = True
    invoice_number: str | None = None


@dataclass
class SaleResult:
    """Normalized outcome of a BIMS sales/add or sales/edit call."""

    kind: SaleResultKind
    data: dict | None = None
    message: str | None = None
    retry_after_ms: int | None = None
    operation_id: str | None = None
