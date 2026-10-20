"""Portal endpoint to reverse one Bancard vPOS payment on demand.

Authentication reuses ``get_portal_tenant``, so a tenant can only ever reach
its own payments. A rollback is a card-network reversal: it never touches the
Shopify order (no refund, no cancellation). It only works on the day of the
operation; a settled charge needs a manual annulment in the Bancard portal.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.payments.factory import build_provider
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.api.portal import get_portal_tenant
from bims_shopify.application.payment_rollback import (
    PaymentIntentNotFoundError,
    RollbackInputError,
    RollbackNotApplicableError,
    RollbackPayment,
    RollbackReport,
)
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import RollbackVerdict, VposConfigurationError
from bims_shopify.domain.tenant import PaymentProvider, Tenant

from .deps import get_audit_logger, get_db_session, get_settings

router = APIRouter(prefix="/api/portal/{slug}/payments", tags=["portal"])

_MESSAGES: dict[str, tuple[str, str]] = {
    "succeeded": (
        "The payment was reversed.",
        "El pago fue revertido.",
    ),
    "no_op": (
        "Nothing to reverse: the payment was never completed or was already reversed.",
        "No hay nada que revertir: el pago nunca se completó o ya fue revertido.",
    ),
    "manual_annulment_required": (
        "The charge is already settled. It can only be annulled manually in the Bancard "
        "commerce portal (Soporte > Transacciones > Solicitar ANULACION).",
        "El cargo ya fue confirmado. Solo puede anularse manualmente en el portal de "
        "comercios de Bancard (Soporte > Transacciones > Solicitar ANULACION).",
    ),
    "transport_failure": (
        "Bancard could not be reached. Nothing was changed; try again.",
        "No se pudo contactar a Bancard. No se modificó nada; intente nuevamente.",
    ),
    "unexpected": (
        "Bancard answered with an unexpected result. Nothing was changed locally.",
        "Bancard respondió con un resultado inesperado. No se modificó nada localmente.",
    ),
}

_ERRORS: dict[str, tuple[int, str, str]] = {
    "missing_identifier": (
        422,
        "Provide an order_id or a shop_process_id.",
        "Indique un order_id o un shop_process_id.",
    ),
    "invalid_shop_process_id": (
        422,
        "shop_process_id must be numeric.",
        "shop_process_id debe ser numérico.",
    ),
    "ambiguous": (
        422,
        "order_id and shop_process_id point to different payments.",
        "order_id y shop_process_id corresponden a pagos distintos.",
    ),
    "payment_not_found": (404, "Payment not found.", "Pago no encontrado."),
    "no_operation": (
        409,
        "This payment has no Bancard operation yet, so there is nothing to roll back.",
        "Este pago aún no tiene una operación en Bancard, por lo que no hay nada que revertir.",
    ),
    "provider_not_supported": (
        409,
        "This store does not use Bancard vPOS.",
        "Esta tienda no usa Bancard vPOS.",
    ),
    "vpos_configuration": (
        409,
        "Bancard rejected the store credentials. Check them in the payments settings.",
        "Bancard rechazó las credenciales de la tienda. Revíselas en la configuración de pagos.",
    ),
}

_VERDICT_STATUS = {
    RollbackVerdict.SUCCEEDED: 200,
    RollbackVerdict.NO_OP: 200,
    RollbackVerdict.MANUAL_ANNULMENT_REQUIRED: 409,
    RollbackVerdict.TRANSPORT_FAILURE: 502,
    RollbackVerdict.UNEXPECTED: 502,
}


class RollbackIn(BaseModel):
    order_id: str | None = None
    shop_process_id: str | None = None


def _error(code: str) -> HTTPException:
    status, message, message_es = _ERRORS[code]
    return HTTPException(
        status_code=status, detail={"code": code, "message": message, "message_es": message_es}
    )


def _body(report: RollbackReport) -> dict[str, Any]:
    message, message_es = _MESSAGES[report.verdict.value]
    return {
        "order_id": report.order_id,
        "shop_process_id": report.shop_process_id,
        "outcome": report.verdict.value,
        "key": report.key,
        "status_before": report.status_before,
        "local_status": report.local_status,
        "manual_action_required": report.verdict is RollbackVerdict.MANUAL_ANNULMENT_REQUIRED,
        "message": message,
        "message_es": message_es,
    }


@router.post("/rollback")
async def rollback_payment(
    body: RollbackIn,
    tenant: Tenant = Depends(get_portal_tenant),
    session: AsyncSession = Depends(get_db_session),
    audit: SqlAlchemyAuditLogger = Depends(get_audit_logger),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    if tenant.payment_provider is not PaymentProvider.BANCARD_VPOS:
        raise _error("provider_not_supported")
    provider = build_provider("bancard_vpos", tenant, settings=settings, audit=audit)
    use_case = RollbackPayment(provider, SqlAlchemyPaymentIntentRepository(session), audit)
    try:
        report = await use_case.run(
            tenant, order_id=body.order_id, shop_process_id=body.shop_process_id, actor="portal"
        )
    except RollbackInputError as exc:
        raise _error(exc.reason) from exc
    except PaymentIntentNotFoundError as exc:
        raise _error("payment_not_found") from exc
    except RollbackNotApplicableError as exc:
        raise _error("no_operation") from exc
    except VposConfigurationError as exc:
        raise _error("vpos_configuration") from exc
    return JSONResponse(_body(report), status_code=_VERDICT_STATUS[report.verdict])
