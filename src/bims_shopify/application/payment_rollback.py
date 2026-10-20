"""On-demand vPOS rollback of one payment, with an auditable, honest outcome.

``abandon_attempt`` is best-effort cleanup and never reports back. This use
case is for an operator who must learn what really happened, so it resolves the
payment unambiguously, asks the provider for the classified outcome, applies a
deliberately small local-status policy and audits every invocation.

Status policy: only a confirmed reversal (``RollbackSuccessful`` or
``AlreadyRollbackedError``) moves the local intent to ``cancelled``. Nothing
else changes, and the Shopify order is never touched: a card reversal is not a
Shopify refund or cancellation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bims_shopify.application.payments import AuditLogPort, PaymentIntentRepositoryPort
from bims_shopify.domain.bancard_vpos import RollbackResult, RollbackVerdict, VposConfigurationError
from bims_shopify.domain.payment import PaymentIntent
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger

logger = get_logger(__name__)

CANCELLED = "cancelled"
_REVERSED_KEYS = frozenset({"RollbackSuccessful", "AlreadyRollbackedError"})
_ORDER_GID_PREFIX = "gid://shopify/Order/"
_MAX_ECHO = 64


class RollbackProviderPort(Protocol):
    name: str

    async def rollback_operation(self, tenant: Tenant, intent: PaymentIntent) -> RollbackResult: ...


class RollbackInputError(Exception):
    """The request did not identify exactly one payment (``reason`` is a stable code)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PaymentIntentNotFoundError(Exception):
    """No payment of this tenant matches the identifiers."""


class RollbackNotApplicableError(Exception):
    """The intent has no Bancard operation yet, so there is nothing to roll back."""


@dataclass(frozen=True)
class RollbackReport:
    order_id: str
    shop_process_id: str
    verdict: RollbackVerdict
    key: str | None
    status_before: str
    local_status: str


def _clean(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _normalise_order_id(order_id: str) -> str:
    return order_id[len(_ORDER_GID_PREFIX):] if order_id.startswith(_ORDER_GID_PREFIX) else order_id


class RollbackPayment:
    def __init__(
        self,
        provider: RollbackProviderPort,
        intents: PaymentIntentRepositoryPort,
        audit: AuditLogPort,
    ) -> None:
        self._provider = provider
        self._intents = intents
        self._audit = audit

    async def run(
        self,
        tenant: Tenant,
        *,
        order_id: str | None,
        shop_process_id: str | None,
        actor: str = "portal",
    ) -> RollbackReport:
        order = _clean(order_id)
        spid = _clean(shop_process_id)
        echo = {"order_id": (order or "")[:_MAX_ECHO], "shop_process_id": (spid or "")[:_MAX_ECHO]}
        try:
            intent = await self._resolve(tenant, order, spid)
        except RollbackInputError as exc:
            await self._record(tenant, actor, echo, f"rejected_{exc.reason}")
            raise
        except PaymentIntentNotFoundError:
            await self._record(tenant, actor, echo, "not_found")
            raise
        if not intent.provider_reference:
            await self._record(tenant, actor, echo, "no_operation")
            raise RollbackNotApplicableError(intent.order_id)

        ids = {"order_id": intent.order_id, "shop_process_id": intent.provider_reference}
        try:
            result = await self._provider.rollback_operation(tenant, intent)
        except VposConfigurationError:
            await self._record(tenant, actor, ids, "configuration_error")
            raise

        before = intent.status
        after = before
        if result.key in _REVERSED_KEYS and before != CANCELLED:
            await self._intents.update_status(
                tenant.id, self._provider.name, intent.order_id, CANCELLED
            )
            after = CANCELLED
        if result.verdict is RollbackVerdict.MANUAL_ANNULMENT_REQUIRED:
            logger.error(
                "vpos_rollback_requires_manual_annulment",
                tenant=tenant.slug,
                order_id=intent.order_id,
                shop_process_id=intent.provider_reference,
            )
            await self._record(
                tenant, actor, ids, result.verdict.value, action="payment.rollback_conflict",
                key=result.key, status_before=before, status_after=after,
            )
        await self._record(
            tenant, actor, ids, result.verdict.value,
            key=result.key, status_before=before, status_after=after,
        )
        return RollbackReport(
            intent.order_id, intent.provider_reference, result.verdict, result.key, before, after
        )

    async def _resolve(self, tenant: Tenant, order: str | None, spid: str | None) -> PaymentIntent:
        if order is None and spid is None:
            raise RollbackInputError("missing_identifier")
        if spid is not None and not spid.isdigit():
            raise RollbackInputError("invalid_shop_process_id")
        name = self._provider.name
        by_order = by_ref = None
        if order is not None:
            by_order = await self._intents.get_by_order_id(
                tenant.id, name, _normalise_order_id(order)
            )
        if spid is not None:
            by_ref = await self._intents.get_by_provider_reference(tenant.id, name, spid)
        if order is not None and spid is not None:
            if by_order is None and by_ref is None:
                raise PaymentIntentNotFoundError(order)
            if by_order is None or by_ref is None or by_order.order_id != by_ref.order_id:
                raise RollbackInputError("ambiguous")
        found = by_order or by_ref
        if found is None:
            raise PaymentIntentNotFoundError(order or spid or "")
        return found

    async def _record(
        self,
        tenant: Tenant,
        actor: str,
        ids: dict[str, str],
        outcome: str,
        *,
        action: str = "payment.rollback_requested",
        key: str | None = None,
        status_before: str | None = None,
        status_after: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {**ids, "outcome": outcome}
        if status_before is not None:
            payload.update(key=key, status_before=status_before, status_after=status_after)
        await self._audit.log(
            actor=actor,
            action=action,
            entity="payment_intent",
            tenant_id=tenant.id,
            entity_id=ids["order_id"] or ids["shop_process_id"] or None,
            payload=payload,
        )
