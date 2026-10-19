"""Direct Bancard vPOS 2.0 payment provider (single_buy, confirm callback, get_confirmation).

``single_buy`` returns a ``process_id``, not a URL, so the checkout URL points
at our own signed iframe page (``checkout_link``). The paid decision is never
taken from the callback body: it comes only from the out-of-band
``get_confirmation`` call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx

from bims_shopify.checkout_link import (
    derive_link_secret,
    sign_checkout_token,
    sign_navigation_token,
)
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import (
    ConfirmationOutcome,
    RollbackOutcome,
    classify_rollback_key,
    rollback_token,
    classify_response_code,
    get_confirmation_token,
    CURRENCY,
    PROBE_SHOP_PROCESS_ID,
    VposAmount,
    VposApiError,
    VposInvalidAmountError,
    VposConfigurationError,
    VposError,
    build_shop_process_id,
    order_id_from_shop_process_id,
    parse_confirm_operation,
    single_buy_token,
    verify_confirm_token,
    vpos_base_url,
    vpos_description,
)
from bims_shopify.domain.payment import PaymentIntent, PaymentStatus
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger

logger = get_logger(__name__)

SINGLE_BUY_PATH = "/vpos/api/0.3/single_buy"
CONFIRMATIONS_PATH = "/vpos/api/0.3/single_buy/confirmations"
ROLLBACK_PATH = "/vpos/api/0.3/single_buy/rollback"
NOT_FOUND_KEYS = frozenset({"BuyNotFoundError", "PaymentNotFoundError"})
LINK_TTL = timedelta(hours=24)
REQUEST_TIMEOUT_SECONDS = 20.0

# Message keys that mean the tenant's credentials or application are wrong.
CONFIGURATION_KEYS = frozenset(
    {
        "InvalidTokenError",
        "InvalidPublicKeyError",
        "UnauthorizedOperationError",
        "ApplicationNotFoundError",
    }
)


@dataclass(frozen=True)
class CredentialCheck:
    """Outcome of a credentials probe; ``message`` never holds secret material."""

    ok: bool
    reason: str
    message: str


_APPLICATION_KEYS = frozenset({"UnauthorizedOperationError", "ApplicationNotFoundError"})


class VposCallbackTokenError(VposError):
    """The confirm callback token does not match; the body is not authentic."""


class AuditPort(Protocol):
    async def log(self, **kwargs: Any) -> None: ...


class BancardVposProvider:
    """PaymentProviderPort implementation for the direct Bancard vPOS 2.0 API."""

    name = "bancard_vpos"
    supports_attempts = True
    always_ack_callback = True

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        audit: AuditPort | None = None,
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._audit = audit

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _credentials(tenant: Tenant) -> tuple[str, str]:
        public_key = (tenant.provider_config or {}).get("public_key")
        private_key = tenant.bancard_vpos_private_key
        if not public_key or not private_key:
            raise VposConfigurationError("vPOS public or private key is not configured")
        return str(public_key), private_key

    @staticmethod
    def _base_url(tenant: Tenant) -> str:
        return vpos_base_url((tenant.provider_config or {}).get("environment"))

    def _link_secret(self) -> str:
        secret = derive_link_secret(self._settings)
        if not secret:
            raise VposConfigurationError("checkout link signing secret is not configured")
        return secret

    async def _post(self, url: str, body: dict[str, Any]) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.post(url, json=body, timeout=REQUEST_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            return await client.post(url, json=body)

    @staticmethod
    def _first_message(data: dict[str, Any]) -> tuple[str, str, str]:
        messages = data.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            first = messages[0]
            return (
                str(first.get("key") or "UnknownError"),
                str(first.get("level") or "error"),
                str(first.get("dsc") or ""),
            )
        return "UnknownError", "error", ""

    def _raise_for_error(self, data: dict[str, Any]) -> None:
        key, level, dsc = self._first_message(data)
        if key in CONFIGURATION_KEYS:
            raise VposConfigurationError(f"vPOS rejected the credentials: {key}")
        raise VposApiError(key, level, dsc)

    # -- port ---------------------------------------------------------------

    def callback_response(self, intent: PaymentIntent | None) -> dict[str, Any]:
        """vPOS expects this exact body for every confirm call (PDF p.45/p.51)."""
        return {"status": "success"}

    async def create_checkout(
        self,
        tenant: Tenant,
        order_id: str,
        amount: float,
        currency: str,
        *,
        attempt: int = 0,
    ) -> PaymentIntent:
        public_key, private_key = self._credentials(tenant)
        base_url = self._base_url(tenant)
        secret = self._link_secret()
        spid = build_shop_process_id(order_id, attempt)
        vpos_amount = VposAmount.from_order_total(amount)
        slug = tenant.slug
        public_base = self._settings.public_base_url.rstrip("/")
        nav_token = sign_navigation_token(secret, slug, str(order_id), ttl=LINK_TTL)
        body = {
            "public_key": public_key,
            "operation": {
                "token": single_buy_token(private_key, spid, vpos_amount, CURRENCY),
                "shop_process_id": spid,
                "amount": vpos_amount.text,
                "currency": CURRENCY,
                "description": vpos_description(f"Order {order_id}"),
                "return_url": f"{public_base}/pay/bancard_vpos/{slug}/return/{nav_token}",
                "cancel_url": f"{public_base}/pay/bancard_vpos/{slug}/cancel/{nav_token}",
            },
        }
        try:
            response = await self._post(f"{base_url}{SINGLE_BUY_PATH}", body)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("vpos_single_buy_failed", tenant=slug, order_id=order_id, error=type(exc).__name__)
            raise VposApiError("TransportError", "error", type(exc).__name__) from exc
        if not isinstance(data, dict) or data.get("status") != "success":
            self._raise_for_error(data if isinstance(data, dict) else {})
        process_id = data.get("process_id")
        if not process_id:
            raise VposApiError("MissingProcessId", "error", "single_buy returned no process_id")
        checkout_token = sign_checkout_token(
            secret, slug, str(order_id), str(process_id), ttl=LINK_TTL
        )
        logger.info("vpos_single_buy_created", tenant=slug, order_id=order_id, shop_process_id=spid)
        return PaymentIntent(
            tenant_slug=slug,
            order_id=str(order_id),
            amount=float(vpos_amount.value),
            currency=currency,
            checkout_url=f"{public_base}/pay/bancard_vpos/{slug}/{checkout_token}",
            provider_reference=str(spid),
        )

    async def check_credentials(self, tenant: Tenant) -> CredentialCheck:
        """Probe the keys with a read-only ``get_confirmation`` on a reserved id.

        A signed, well-formed answer, even "not found", proves the public key,
        private key and environment work. No payment is created or rolled back.
        """
        try:
            public_key, private_key = self._credentials(tenant)
            base_url = self._base_url(tenant)
        except VposConfigurationError:
            return CredentialCheck(False, "not_configured", "Public key, private key and environment must be saved first.")
        body = {
            "public_key": public_key,
            "operation": {
                "token": get_confirmation_token(private_key, PROBE_SHOP_PROCESS_ID),
                "shop_process_id": PROBE_SHOP_PROCESS_ID,
            },
        }
        try:
            response = await self._post(f"{base_url}{CONFIRMATIONS_PATH}", body)
            if response.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=response.request, response=response)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return CredentialCheck(False, "unreachable", "Bancard could not be reached. Try again later.")
        if not isinstance(data, dict):
            return CredentialCheck(False, "unexpected", "Bancard returned an unexpected response.")
        if data.get("status") == "success":
            return CredentialCheck(True, "ok", "Credentials accepted by Bancard.")
        key, _, _ = self._first_message(data)
        if key in NOT_FOUND_KEYS:
            return CredentialCheck(True, "ok", "Credentials accepted by Bancard.")
        if key in _APPLICATION_KEYS:
            return CredentialCheck(False, "application_not_enabled", "Bancard rejected the application; check that it is enabled for this environment.")
        if key in CONFIGURATION_KEYS:
            return CredentialCheck(False, "invalid_credentials", "Bancard rejected the public or private key.")
        logger.warning("vpos_probe_unexpected_key", tenant=tenant.slug, key=key)
        return CredentialCheck(False, "unexpected", "Bancard returned an unexpected response.")

    async def abandon_attempt(self, tenant: Tenant, intent: PaymentIntent) -> None:
        """Best-effort ``rollback`` of an abandoned attempt; never raises.

        ``PaymentNotFoundError`` is the expected answer for a cart nobody paid,
        so benign outcomes stay silent. ``TransactionAlreadyConfirmed`` means
        the charge settled and only a manual annulment in the Bancard comercios
        portal can reverse it, so it is an error plus an audit entry.
        """
        try:
            await self._rollback(tenant, intent)
        except Exception as exc:
            logger.warning(
                "vpos_rollback_failed",
                tenant=tenant.slug,
                order_id=intent.order_id,
                shop_process_id=intent.provider_reference,
                error=type(exc).__name__,
            )

    async def _rollback(self, tenant: Tenant, intent: PaymentIntent) -> None:
        public_key, private_key = self._credentials(tenant)
        spid = int(intent.provider_reference or "")
        body = {
            "public_key": public_key,
            "elements": {},
            "operation": {
                "token": rollback_token(private_key, spid),
                # Rollback is the one operation that takes the id as a string.
                "shop_process_id": str(spid),
            },
        }
        response = await self._post(f"{self._base_url(tenant)}{ROLLBACK_PATH}", body)
        if response.status_code >= 500:
            raise httpx.HTTPStatusError("server error", request=response.request, response=response)
        data = response.json()
        key, _, _ = self._first_message(data if isinstance(data, dict) else {})
        outcome = classify_rollback_key(key)
        if outcome is RollbackOutcome.BENIGN:
            logger.debug("vpos_rollback_done", tenant=tenant.slug, shop_process_id=spid, key=key)
        elif outcome is RollbackOutcome.OPERATOR_ACTION:
            logger.error(
                "vpos_rollback_requires_manual_annulment",
                tenant=tenant.slug,
                order_id=intent.order_id,
                shop_process_id=spid,
                action="request the annulment in the Bancard comercios portal",
            )
            if self._audit is not None:
                await self._audit.log(
                    actor="system",
                    action="payment.rollback_conflict",
                    entity="payment_intent",
                    tenant_id=tenant.id,
                    entity_id=str(spid),
                    payload={"order_id": intent.order_id, "shop_process_id": spid},
                )
        else:
            logger.warning(
                "vpos_rollback_unexpected_outcome",
                tenant=tenant.slug,
                order_id=intent.order_id,
                shop_process_id=spid,
                key=key,
            )

    async def parse_callback(self, tenant: Tenant, payload: dict[str, Any]) -> PaymentIntent:
        """Authenticate and coerce a confirm callback; never decides the paid state."""
        _, private_key = self._credentials(tenant)
        operation = payload.get("operation") if isinstance(payload, dict) else None
        callback = parse_confirm_operation(operation)
        if not verify_confirm_token(private_key, callback):
            logger.warning("vpos_callback_token_mismatch", tenant=tenant.slug)
            raise VposCallbackTokenError("confirm callback token does not match")
        return PaymentIntent(
            tenant_slug=tenant.slug,
            order_id=order_id_from_shop_process_id(callback.shop_process_id),
            amount=float(callback.amount.value),
            currency=callback.currency,
            provider_reference=str(callback.shop_process_id),
            status="pending",
        )

    async def verify_payment(self, tenant: Tenant, intent: PaymentIntent) -> PaymentStatus:
        """Ask vPOS (get_confirmation) whether the payment was approved.

        Paid only for ``response_code == "00"`` with a matching amount and
        currency. Transport problems and unknown outcomes leave the intent
        pending; credential problems raise ``VposConfigurationError`` so they
        are never recorded as a customer failure.
        """
        public_key, private_key = self._credentials(tenant)
        base_url = self._base_url(tenant)
        try:
            spid = int(intent.provider_reference or "")
        except ValueError as exc:
            raise VposConfigurationError("intent has no valid shop_process_id") from exc
        body = {
            "public_key": public_key,
            "operation": {
                "token": get_confirmation_token(private_key, spid),
                "shop_process_id": spid,
            },
        }
        try:
            response = await self._post(f"{base_url}{CONFIRMATIONS_PATH}", body)
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    "server error", request=response.request, response=response
                )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "vpos_get_confirmation_unavailable",
                tenant=tenant.slug,
                shop_process_id=spid,
                error=type(exc).__name__,
            )
            return PaymentStatus.PENDING
        if not isinstance(data, dict):
            logger.warning("vpos_get_confirmation_bad_shape", tenant=tenant.slug, shop_process_id=spid)
            return PaymentStatus.PENDING
        if data.get("status") != "success":
            return self._error_status(tenant, spid, data)
        confirmation = data.get("confirmation")
        if not isinstance(confirmation, dict):
            logger.warning("vpos_get_confirmation_missing", tenant=tenant.slug, shop_process_id=spid)
            return PaymentStatus.PENDING
        code = confirmation.get("response_code")
        outcome = classify_response_code(code)
        if outcome is ConfirmationOutcome.DECLINED:
            logger.info("vpos_payment_declined", tenant=tenant.slug, shop_process_id=spid, response_code=code)
            return PaymentStatus.FAILED
        if outcome is ConfirmationOutcome.UNKNOWN:
            logger.warning("vpos_unknown_response_code", tenant=tenant.slug, shop_process_id=spid, response_code=code)
            return PaymentStatus.PENDING
        if not await self._matches_intent(tenant, spid, intent, confirmation):
            return PaymentStatus.FAILED
        logger.info("vpos_payment_confirmed", tenant=tenant.slug, shop_process_id=spid)
        return PaymentStatus.CONFIRMED

    def _error_status(self, tenant: Tenant, spid: int, data: dict[str, Any]) -> PaymentStatus:
        key, _, _ = self._first_message(data)
        if key in CONFIGURATION_KEYS:
            raise VposConfigurationError(f"vPOS rejected the credentials: {key}")
        if key in NOT_FOUND_KEYS:
            logger.info("vpos_payment_not_found", tenant=tenant.slug, shop_process_id=spid, key=key)
            return PaymentStatus.FAILED
        logger.warning("vpos_get_confirmation_error", tenant=tenant.slug, shop_process_id=spid, key=key)
        return PaymentStatus.PENDING

    async def _matches_intent(
        self, tenant: Tenant, spid: int, intent: PaymentIntent, confirmation: dict[str, Any]
    ) -> bool:
        try:
            confirmed = VposAmount.from_vpos_payload(confirmation.get("amount")).value
            expected = VposAmount.from_order_total(Decimal(str(intent.amount))).value
        except VposInvalidAmountError:
            confirmed = expected = None
        currency_ok = confirmation.get("currency") == intent.currency
        if confirmed is not None and confirmed == expected and currency_ok:
            return True
        logger.error(
            "vpos_amount_mismatch",
            tenant=tenant.slug,
            shop_process_id=spid,
            expected=str(expected),
            confirmed=str(confirmed),
        )
        if self._audit is not None:
            await self._audit.log(
                actor="system",
                action="payment.amount_mismatch",
                entity="payment_intent",
                tenant_id=tenant.id,
                entity_id=str(spid),
                payload={"order_id": intent.order_id, "shop_process_id": spid},
            )
        return False
