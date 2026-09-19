"""Use cases: create a hosted-checkout payment link and handle its callback."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from bims_shopify.domain.payment import PaymentIntent, PaymentStatus
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ports.payment import PaymentProviderPort
from bims_shopify.ports.storefront import StorefrontPort


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


class CreatePaymentLink:
    """Create (or reuse) a hosted-checkout link for a Shopify order.

    Idempotent per (tenant, provider, order_id): a retried webhook delivery
    or a merchant re-opening the portal must not mint a second Pagopar
    transaction for the same order, so an existing persisted intent with a
    checkout_url is returned as-is instead of calling the provider again.
    """

    def __init__(self, provider: PaymentProviderPort, intents: PaymentIntentRepositoryPort) -> None:
        self._provider = provider
        self._intents = intents

    async def run(self, tenant: Tenant, order_id: str, amount: float, currency: str) -> PaymentIntent:
        existing = await self._intents.get_by_order_id(tenant.id, self._provider.name, order_id)
        if existing is not None and existing.checkout_url:
            return existing

        intent = await self._provider.create_checkout(tenant, order_id, amount, currency)
        await self._intents.save(tenant.id, self._provider.name, intent)
        return intent


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
