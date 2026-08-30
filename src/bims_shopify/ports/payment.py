"""Port describing a hosted-checkout style payment provider."""
from __future__ import annotations

from typing import Any, Protocol

from bims_shopify.domain.payment import PaymentIntent, PaymentStatus
from bims_shopify.domain.tenant import Tenant


class PaymentProviderPort(Protocol):
    name: str

    async def create_checkout(
        self, tenant: Tenant, order_id: str, amount: float, currency: str
    ) -> PaymentIntent:
        """Create a hosted checkout link the customer will be redirected to."""
        ...

    async def parse_callback(self, tenant: Tenant, payload: dict[str, Any]) -> PaymentIntent:
        """Parse a provider webhook/callback payload into a PaymentIntent update."""
        ...

    async def verify_payment(self, tenant: Tenant, intent: PaymentIntent) -> PaymentStatus:
        """Verify the payment status directly with the provider.

        The callback body itself must never be trusted for the paid/confirmed
        decision (it can be spoofed by anyone who knows/guesses the callback
        URL); this makes an authenticated out-of-band call to the provider.
        """
        ...
