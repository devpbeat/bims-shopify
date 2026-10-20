"""Use cases: create a hosted-checkout payment link and handle its callback."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from bims_shopify.domain.bancard_vpos import (
    MAX_ATTEMPT,
    VposAmount,
    VposAttemptsExhaustedError,
)
from bims_shopify.domain.payment import PaymentIntent, PaymentStatus
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger
from bims_shopify.ports.payment import PaymentProviderPort
from bims_shopify.ports.storefront import StorefrontPort

logger = get_logger(__name__)


class OrderAmountError(Exception):
    """The Shopify order cannot be charged (missing, unreadable total, or wrong currency)."""

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


@dataclass(frozen=True)
class LiveOrder:
    """What the storefront says about an order right now."""

    amount: float
    paid: bool
    status_url: str | None


async def resolve_live_order(
    tenant: Tenant, order_id: str, storefront: StorefrontPort, *, currency: str = "PYG"
) -> LiveOrder:
    """Read the chargeable total (and paid state) from the Shopify order itself.

    Raises ``OrderAmountError`` if the order is missing or not priced in
    ``currency``; storefront transport errors propagate to the caller.
    """
    gid = order_id if order_id.startswith("gid://") else f"gid://shopify/Order/{order_id}"
    order = await storefront.get_order(tenant, gid)
    if order is None:
        raise OrderAmountError("order not found", not_found=True)
    money = (order.get("totalPriceSet") or {}).get("shopMoney") or {}
    if money.get("currencyCode") != currency:
        raise OrderAmountError(f"order is not priced in {currency}")
    try:
        total = Decimal(str(money.get("amount")))
    except InvalidOperation as exc:
        raise OrderAmountError("order total is unreadable") from exc
    if not total.is_finite() or total <= 0:
        raise OrderAmountError("order total is not chargeable")
    status_url = order.get("statusPageUrl")
    return LiveOrder(
        amount=float(total),
        paid=order.get("displayFinancialStatus") == "PAID",
        status_url=status_url if isinstance(status_url, str) and status_url else None,
    )


async def resolve_order_amount(
    tenant: Tenant, order_id: str, storefront: StorefrontPort, *, currency: str = "PYG"
) -> float:
    """Charge amount read from the Shopify order itself, never from a request body."""
    return (await resolve_live_order(tenant, order_id, storefront, currency=currency)).amount


class PaymentIntentRepositoryPort(Protocol):
    """Persistence for created payment links.

    Needed because Pagopar's callback never echoes the merchant's own order
    id (see `PagoparProvider.parse_callback`) — the callback handler must
    resolve `order_id` from a row saved here, keyed by `provider_reference`.
    Also backs the merchant-portal "recent payments" listing.
    """

    async def get_by_order_id(
        self, tenant_id: int, provider: str, order_id: str
    ) -> PaymentIntent | None: ...

    async def get_by_provider_reference(
        self, tenant_id: int, provider: str, provider_reference: str
    ) -> PaymentIntent | None: ...

    async def save(self, tenant_id: int, provider: str, intent: PaymentIntent) -> None: ...

    async def update_status(
        self, tenant_id: int, provider: str, order_id: str, status: str
    ) -> None: ...


class AuditLogPort(Protocol):
    async def log(self, **kwargs: Any) -> None: ...


class CreatePaymentLink:
    """Create (or reuse) a hosted-checkout link for a Shopify order.

    Idempotent per (tenant, provider, order_id): a retried webhook delivery
    or a merchant re-opening the portal must not mint a second Pagopar
    transaction for the same order, so an existing persisted intent with a
    checkout_url is returned as-is instead of calling the provider again.

    Providers that set ``supports_attempts`` (vPOS) follow a stricter policy:
    the existing intent is reused while it is ``pending`` or ``paid``; only a
    ``failed`` or ``cancelled`` one mints the next attempt, after a
    best-effort ``abandon_attempt`` on the old one that never blocks the retry.
    """

    def __init__(
        self,
        provider: PaymentProviderPort,
        intents: PaymentIntentRepositoryPort,
        audit: AuditLogPort | None = None,
    ) -> None:
        self._provider = provider
        self._intents = intents
        self._audit = audit

    async def run(self, tenant: Tenant, order_id: str, amount: float, currency: str) -> PaymentIntent:
        existing = await self._intents.get_by_order_id(tenant.id, self._provider.name, order_id)
        if not getattr(self._provider, "supports_attempts", False):
            if existing is not None and existing.checkout_url:
                return existing
            intent = await self._provider.create_checkout(tenant, order_id, amount, currency)
            await self._intents.save(tenant.id, self._provider.name, intent)
            return intent
        return await self._run_with_attempts(tenant, order_id, amount, currency, existing)

    async def _run_with_attempts(
        self,
        tenant: Tenant,
        order_id: str,
        amount: float,
        currency: str,
        existing: PaymentIntent | None,
    ) -> PaymentIntent:
        provider: Any = self._provider
        attempt = 0
        abandoned: PaymentIntent | None = None
        if existing is not None:
            attempt = _attempt_of(existing)
            if existing.status == "paid":
                return existing
            if existing.status == "pending":
                if existing.provider_reference and existing.checkout_url:
                    if existing.process_id and _same_amount(existing.amount, amount):
                        return existing
                    # The order total moved (or the operation predates stored
                    # process ids): roll back the stale operation, mint the next.
                    if attempt >= MAX_ATTEMPT:
                        raise VposAttemptsExhaustedError("no attempts left for this order")
                    attempt += 1
                    abandoned = existing
            elif existing.status in _RETRYABLE_STATUSES:
                if attempt >= MAX_ATTEMPT:
                    raise VposAttemptsExhaustedError("no attempts left for this order")
                attempt += 1
                abandoned = existing
        if abandoned is not None:
            try:
                await provider.abandon_attempt(tenant, abandoned)
            except Exception as exc:  # a rollback outcome must never block the retry
                logger.warning(
                    "payment_abandon_failed",
                    tenant=tenant.slug,
                    order_id=order_id,
                    error=type(exc).__name__,
                )
        intent = await provider.create_checkout(tenant, order_id, amount, currency, attempt=attempt)
        await self._intents.save(tenant.id, self._provider.name, intent)
        if abandoned is not None and self._audit is not None:
            await self._audit.log(
                actor="system",
                action="payment.retry_minted",
                entity="order",
                tenant_id=tenant.id,
                entity_id=order_id,
                payload={
                    "attempt": attempt,
                    "previous_status": abandoned.status,
                    "previous_reference": abandoned.provider_reference,
                },
            )
        return intent


_RETRYABLE_STATUSES = frozenset({"failed", "cancelled"})


def _same_amount(stored: float, live: float) -> bool:
    """Compare as the wire does: rounded to the two decimals vPOS is charged."""
    try:
        return VposAmount.from_order_total(stored).value == VposAmount.from_order_total(live).value
    except ValueError:
        return False


def _attempt_of(intent: PaymentIntent) -> int:
    """Attempt number encoded in the last two digits of the stored reference."""
    try:
        return int(intent.provider_reference or "0") % 100
    except ValueError:
        return 0


class ProcessedEventRepositoryPort(Protocol):
    """Minimal slice of the sync-state repository used for callback idempotency."""

    async def get_event_status(self, tenant_id: int, source: str, external_id: str) -> str | None: ...

    async def try_claim_event(
        self, tenant_id: int, source: str, external_id: str, payload_hash: str
    ) -> bool: ...

    async def mark_event_status(
        self, tenant_id: int, source: str, external_id: str, status: str
    ) -> None: ...

    async def delete_event(self, tenant_id: int, source: str, external_id: str) -> None: ...


class HandlePaymentCallback:
    """Handle a payment provider webhook/callback.

    The callback body is never trusted for the paid/confirmed decision: it is
    only used to identify which payment intent the callback refers to, and
    the actual status is always re-verified out-of-band via
    `provider.verify_payment`. This closes a spoofing hole where anyone who
    knows/guesses the callback URL could POST a fake "success" payload and
    get an order marked as paid for free.

    Idempotency uses the same atomic `try_claim_event` pattern as the
    Shopify webhook handler, keyed by `source="payment:<provider>"` and the
    payment's provider reference (or order id as a fallback). Claiming
    relies on a DB-level unique constraint, so concurrent redeliveries of
    the same callback race on the INSERT itself instead of a racy
    SELECT-then-INSERT check, and never re-verify or re-mark an order as
    paid twice.
    """

    def __init__(
        self,
        provider: PaymentProviderPort,
        storefront: StorefrontPort,
        processed_events: ProcessedEventRepositoryPort,
        intents: PaymentIntentRepositoryPort | None = None,
    ) -> None:
        self._provider = provider
        self._storefront = storefront
        self._processed_events = processed_events
        self._intents = intents

    async def run(self, tenant: Tenant, payload: dict[str, Any]) -> PaymentIntent:
        intent = await self._provider.parse_callback(tenant, payload)

        if not intent.order_id and intent.provider_reference and self._intents is not None:
            # Pagopar's callback never echoes the merchant's own order id
            # (only its internal hash_pedido/numero_pedido) — resolve it
            # from the intent persisted at checkout time, keyed by the
            # shared provider_reference (hash_pedido).
            stored = await self._intents.get_by_provider_reference(
                tenant.id, self._provider.name, intent.provider_reference
            )
            if stored is not None:
                intent.order_id = stored.order_id

        external_id = intent.provider_reference or intent.order_id
        source = f"payment:{self._provider.name}"
        payload_hash = _hash_payload(payload)

        claimed = await self._processed_events.try_claim_event(
            tenant.id, source, external_id, payload_hash
        )
        if not claimed:
            previous_status = await self._processed_events.get_event_status(
                tenant.id, source, external_id
            )
            if previous_status is not None:
                intent.status = previous_status
            return intent

        try:
            verified_status = await self._provider.verify_payment(tenant, intent)
            if verified_status == PaymentStatus.CONFIRMED and intent.order_id:
                await self._storefront.mark_order_as_paid(tenant, intent.order_id)
                intent.status = "paid"
            elif verified_status == PaymentStatus.PENDING:
                intent.status = "pending"
            else:
                intent.status = "failed"
        except Exception:
            # Release the claim so a future retry/redelivery of this
            # callback can attempt verification again instead of being
            # silently swallowed as "already processed".
            await self._processed_events.delete_event(tenant.id, source, external_id)
            raise

        await self._processed_events.mark_event_status(
            tenant.id, source, external_id, intent.status
        )
        if self._intents is not None and intent.order_id:
            await self._intents.update_status(
                tenant.id, self._provider.name, intent.order_id, intent.status
            )
        return intent


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
