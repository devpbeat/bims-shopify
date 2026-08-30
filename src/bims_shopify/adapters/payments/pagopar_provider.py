"""Pagopar hosted-checkout payment provider.

Uses the optional `pagopar-sdk` PyPI package (sync httpx.Client under the
hood, so calls are dispatched via `asyncio.to_thread`). The import is
guarded so the application still runs when that extra is not installed,
unless a tenant is actually configured to use Pagopar.

SDK surface used (pagopar_sdk==0.1.1, read from
`.venv/lib/python3.12/site-packages/pagopar_sdk/`):
  - `PagoparClient(public_key=, private_key=, base_url=)` — top-level client.
    Default `base_url` is `https://api.pagopar.com`; Pagopar does not
    document a separate sandbox host — "test mode" is a distinct
    public/private key pair used against the same production endpoint
    (`base_url` is kept overridable via `provider_config` regardless).
  - `client.commerce.create_transaction(StartTransactionRequest | dict)` ->
    POSTs `/api/comercios/2.0/iniciar-transaccion`. The SDK computes the
    SHA1 `token` itself (`build_start_transaction_token`); callers never
    build it manually.
  - `client.commerce.get_order(hash_pedido)` -> POSTs `/api/pedidos/1.1/traer`
    to fetch the current order status. The SDK computes `token` via
    `build_token(private_key, "CONSULTA")`.
  - `pagopar_sdk.build_token(private_key, suffix)` -> SHA1(private_key + suffix)
    is Pagopar's generic token scheme; the same primitive is used here to
    validate the `token` field Pagopar sends on its confirmation callback
    (SHA1(private_key + hash_pedido)).

Documented response shapes (source: Pagopar's official integration guide,
"API - Integracion de medios de pagos",
https://soporte.pagopar.com/portal/es/kb/articles/api-integracion-medios-pagos,
retrieved 2026-08 — verified against two independent fetches that quoted
matching verbatim JSON examples):

  - `create_transaction` response: `{"respuesta": true, "resultado": [
    {"data": "<hash_pedido>", "pedido": "<pagopar_order_number>"}]}`.
    `resultado[0]["data"]` IS the `hash_pedido` string directly — it is
    *not* a nested object. The hosted checkout page the buyer is sent to is
    `https://www.pagopar.com/pagos/{hash_pedido}`.
  - Payment confirmation callback (POST to the merchant's response URL) and
    `get_order` (`/api/pedidos/1.1/traer`) share the same `resultado[0]`
    item shape: `{"pagado": bool, "cancelado": bool, "hash_pedido": str,
    "numero_pedido": str, "forma_pago": str, "forma_pago_identificador":
    str, "monto": str, "fecha_pago": str | None, "token": str, ...}`.
    There is **no `estado` field** — `pagado: true` is the sole documented
    "paid" indicator, and `cancelado: true` marks a cancelled/failed order.
    Neither payload includes the merchant's own `id_pedido_comercio`;
    only Pagopar's internal `numero_pedido` and the shared `hash_pedido`
    identify the order, so callers must resolve the Shopify/merchant order
    id from `hash_pedido` (the `provider_reference` stored at checkout
    time) rather than from the callback body.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from bims_shopify.domain.payment import PaymentIntent, PaymentStatus
from bims_shopify.domain.tenant import Tenant

try:  # pragma: no cover - depends on optional extra being installed
    import pagopar_sdk
    from pagopar_sdk import (
        Buyer,
        PagoparClient,
        PurchaseItem,
        StartTransactionRequest,
    )
except ImportError:  # pragma: no cover
    pagopar_sdk = None
    Buyer = None  # type: ignore[assignment]
    PagoparClient = None  # type: ignore[assignment]
    PurchaseItem = None  # type: ignore[assignment]
    StartTransactionRequest = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "https://api.pagopar.com"
DEFAULT_CHECKOUT_URL_TEMPLATE = "https://www.pagopar.com/pagos/{hash_pedido}"

class PagoparNotInstalledError(RuntimeError):
    pass

class PagoparCallbackTokenError(ValueError):
    """Raised when a Pagopar callback's token does not match the expected SHA1 signature."""

class PagoparProvider:
    """PaymentProviderPort implementation backed by Pagopar's hosted checkout."""

    name = "pagopar"

    def _require_sdk(self) -> Any:
        if pagopar_sdk is None:
            raise PagoparNotInstalledError(
                "pagopar-sdk is not installed; install the 'pagopar' extra to use this provider."
            )
        return pagopar_sdk

    def _build_client(self, tenant: Tenant) -> PagoparClient:
        self._require_sdk()
        config = tenant.provider_config or {}
        base_url = config.get("base_url") or DEFAULT_BASE_URL
        return PagoparClient(
            public_key=config.get("public_key", ""),
            private_key=config.get("private_key", ""),
            base_url=base_url,
        )

    async def create_checkout(
        self, tenant: Tenant, order_id: str, amount: float, currency: str
    ) -> PaymentIntent:
        self._require_sdk()
        config = tenant.provider_config or {}
        client = self._build_client(tenant)

        buyer_cfg = config.get("buyer", {})
        buyer = Buyer(
            nombre=buyer_cfg.get("nombre", "Cliente"),
            email=buyer_cfg.get("email", "cliente@example.com"),
            documento=buyer_cfg.get("documento", "0"),
            telefono=buyer_cfg.get("telefono", ""),
            ciudad=buyer_cfg.get("ciudad", "1"),
        )
        item = PurchaseItem(
            id_producto=order_id,
            nombre=f"Order {order_id}",
            descripcion=config.get("description", f"Shopify order {order_id}"),
            cantidad=1,
            precio_total=amount,
            categoria=config.get("category_id", "1"),
            ciudad=buyer_cfg.get("ciudad", "1"),
            url_imagen=config.get("image_url", ""),
        )
        fecha_maxima_pago = (datetime.now(UTC) + timedelta(days=7)).strftime(
            "%d-%m-%Y %H:%M:%S"
        )
        request = StartTransactionRequest(
            id_pedido_comercio=order_id,
            monto_total=amount,
            tipo_pedido="single",
            forma_pago=config.get("payment_method_id", 1),
            comprador=buyer,
            compras_items=[item],
            fecha_maxima_pago=fecha_maxima_pago,
            descripcion_resumen=config.get("description", f"Shopify order {order_id}"),
        )

        try:
            response = await asyncio.to_thread(client.commerce.create_transaction, request)
        finally:
            client.close()

        resultado = (response.get("resultado") or [{}])[0]
        # `resultado[0]["data"]` IS the hash_pedido string itself (Pagopar's
        # docs), not a nested object with a "hash_pedido" key.
        hash_pedido = resultado.get("data")
        checkout_url_template = config.get(
            "checkout_url_template", DEFAULT_CHECKOUT_URL_TEMPLATE
        )
        checkout_url = (
            checkout_url_template.format(hash_pedido=hash_pedido) if hash_pedido else None
        )

        return PaymentIntent(
            tenant_slug=tenant.slug,
            order_id=order_id,
            amount=amount,
            currency=currency,
            checkout_url=checkout_url,
            provider_reference=hash_pedido,
        )

    async def parse_callback(self, tenant: Tenant, payload: dict[str, Any]) -> PaymentIntent:
        """Parse Pagopar's confirmation callback.

        Pagopar POSTs `{"respuesta": bool, "resultado": [{"hash_pedido":
        ..., "token": ..., "pagado": bool, "monto": "...", ...}]}` — the
        same item shape returned by `get_order`. `hash_pedido` (accepted
        either nested under `resultado[0]` or, defensively, at the top
        level) is the only identifier used to locate the order; the
        `pagado`/`cancelado` fields in this payload are attacker-reachable
        and are never trusted (see `verify_payment`). If a `token` field is
        present, it is validated against Pagopar's
        SHA1(private_key + hash_pedido) scheme so requests without a
        matching signature don't even lead to a live order lookup.

        Pagopar does not echo the merchant's own order id
        (`id_pedido_comercio`) in this callback — only its internal
        `numero_pedido` and the shared `hash_pedido` are present — so
        `order_id` is left empty here; callers resolve the merchant order
        from `provider_reference` (`hash_pedido`) instead.
        """
        self._require_sdk()
        config = tenant.provider_config or {}
        resultado = payload.get("resultado") or [{}]
        item = resultado[0] if isinstance(resultado, list) and resultado else {}

        hash_pedido = payload.get("hash_pedido") or item.get("hash_pedido")
        token = payload.get("token") if "token" in payload else item.get("token")
        if token is not None:
            expected = pagopar_sdk.build_token(config.get("private_key", ""), str(hash_pedido))
            if token != expected:
                raise PagoparCallbackTokenError("Pagopar callback token does not match.")

        order_id = str(payload.get("id_pedido_comercio") or item.get("id_pedido_comercio") or "")
        monto = payload.get("monto") if "monto" in payload else item.get("monto")
        return PaymentIntent(
            tenant_slug=tenant.slug,
            order_id=order_id,
            amount=float(monto or 0),
            currency=payload.get("currency", "PYG"),
            provider_reference=hash_pedido,
            status="pending",
        )

    async def verify_payment(self, tenant: Tenant, intent: PaymentIntent) -> PaymentStatus:
        """Query Pagopar directly for the order's real status via `get_order`.

        The callback body's apparent status is attacker-controlled (anyone
        who knows/guesses the callback URL can POST it), so it must never
        be the source of truth for marking an order paid. This looks the
        order up by `hash_pedido` instead of trusting the payload.
        """
        client = self._build_client(tenant)
        try:
            response = await asyncio.to_thread(client.commerce.get_order, intent.provider_reference or "")
        except pagopar_sdk.PagoparError:
            return PaymentStatus.FAILED
        finally:
            client.close()

        resultado = (response.get("resultado") or [{}])[0]
        # Pagopar's `traer` response has no `estado` field: `pagado` and
        # `cancelado` booleans are the only documented status indicators.
        if bool(resultado.get("pagado")):
            return PaymentStatus.CONFIRMED
        if bool(resultado.get("cancelado")):
            return PaymentStatus.FAILED
        return PaymentStatus.PENDING
