"""Bancard payment provider proxied through BIMS (bims_pay + bancard_transactions endpoints).

The BIMS OpenAPI spec (see docs/bims-api-notes.md) documents `POST
/api/bims_pay/create_link.json` and `GET
/api/bancard_transactions/lookup/{shopProcessId}.json` only as generic,
untyped `application/x-www-form-urlencoded` endpoints — it does not
enumerate Bancard's actual field names. Those are therefore taken from
tenant.provider_config:

- `create_link_fields`: dict of `{bims_field_name: template_string}`, where
  each template string may contain `{amount}`, `{currency}`, `{order_id}`,
  `{description}` and `{return_url}` placeholders. This lets each tenant
  match whatever field names their BIMS/Bancard integration actually
  expects (e.g. `{"monto": "{amount}", "moneda": "{currency}",
  "id_pedido": "{order_id}"}`) without code changes.
- `confirmed_status_values`: list of string status values (from the
  `bancard_transactions/lookup` response) that count as a confirmed
  payment, e.g. `["confirmed", "paid", "success"]`. Configurable because
  the spec does not document Bancard's actual status vocabulary.
- `return_url`: URL Bancard/BIMS should redirect the buyer to after payment.

`shop_process_id` (the path param on the lookup endpoint) is the Shopify
order number, not BIMS' or Bancard's own transaction id — this lets the
lookup happen without needing anything back from the create_link response.
"""
from __future__ import annotations

from typing import Any

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.domain.payment import PaymentIntent, PaymentStatus
from bims_shopify.domain.tenant import Tenant

DEFAULT_CONFIRMED_STATUS_VALUES = ("confirmed", "paid", "success", "pagado")
DEFAULT_PENDING_STATUS_VALUES = ("pending", "processing", "pendiente")

class BancardViaBIMSProvider:
    """PaymentProviderPort implementation that delegates to BIMS Bancard endpoints."""

    name = "bancard"

    def __init__(self, client: BIMSClient) -> None:
        self._client = client

    def _render_create_link_fields(
        self, config: dict[str, Any], order_id: str, amount: float, currency: str
    ) -> dict[str, Any]:
        template: dict[str, Any] = config.get("create_link_fields") or {}
        placeholders = {
            "amount": amount,
            "currency": currency,
            "order_id": order_id,
            "description": config.get("description_template", "Order {order_id}").format(
                order_id=order_id
            ),
            "return_url": config.get("return_url", ""),
        }
        rendered: dict[str, Any] = {}
        for field_name, value in template.items():
            if isinstance(value, str):
                rendered[field_name] = value.format(**placeholders)
            else:
                rendered[field_name] = value
        return rendered

    async def create_checkout(
        self, tenant: Tenant, order_id: str, amount: float, currency: str
    ) -> PaymentIntent:
        config = tenant.provider_config or {}
        payload = self._render_create_link_fields(config, order_id, amount, currency)
        response = await self._client.bims_pay_create_link(payload)
        data = response.get("data") or {}
        return PaymentIntent(
            tenant_slug=tenant.slug,
            order_id=order_id,
            amount=amount,
            currency=currency,
            checkout_url=data.get("link_url") or data.get("url"),
            # shop_process_id used by bancard_transactions/lookup is the
            # Shopify order number itself, not any id BIMS/Bancard returns.
            provider_reference=order_id,
        )

    async def parse_callback(self, tenant: Tenant, payload: dict[str, Any]) -> PaymentIntent:
        """Parse either callback shape BIMS/Bancard may deliver.

        Pagopar-style JSON POST bodies use `order_id`/`amount`/`currency`.
        Bancard's confirmation may instead arrive as a GET with query
        params surfaced by the route as a plain dict (e.g.
        `shop_process_id`). Either way only the identifier is used here —
        the status is always re-verified with `verify_payment`.
        """
        order_id = str(payload.get("order_id") or payload.get("shop_process_id") or "")
        return PaymentIntent(
            tenant_slug=tenant.slug,
            order_id=order_id,
            amount=float(payload.get("amount", 0) or 0),
            currency=payload.get("currency", "PYG"),
            provider_reference=order_id,
            status="pending",
        )

    async def verify_payment(self, tenant: Tenant, intent: PaymentIntent) -> PaymentStatus:
        """Look the transaction up directly with BIMS instead of trusting the callback body.

        The callback body's `status` field is attacker-controlled (anyone who
        knows/guesses the callback URL can POST it), so it must never be the
        source of truth for marking an order paid.
        """
        config = tenant.provider_config or {}
        confirmed_values = {
            str(v).lower()
            for v in config.get("confirmed_status_values", DEFAULT_CONFIRMED_STATUS_VALUES)
        }
        pending_values = {
            str(v).lower()
            for v in config.get("pending_status_values", DEFAULT_PENDING_STATUS_VALUES)
        }

        shop_process_id = intent.provider_reference or intent.order_id
        response = await self._client.lookup_bancard_transaction(shop_process_id)
        data = response.get("data") or {}
        status_value = str(data.get("status", "")).lower()

        if data.get("confirmed") is True or status_value in confirmed_values:
            return PaymentStatus.CONFIRMED
        if status_value in pending_values:
            return PaymentStatus.PENDING
        return PaymentStatus.FAILED
