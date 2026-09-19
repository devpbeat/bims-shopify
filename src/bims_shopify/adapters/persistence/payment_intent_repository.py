"""SQLAlchemy-backed repository for persisted hosted-checkout payment intents.

Persisting the intent (rather than only relying on `processed_events` for
idempotency) exists for two reasons: Pagopar's callback never echoes the
merchant's own Shopify order id (only its internal `hash_pedido`), so the
callback must resolve `order_id` from a row keyed by `provider_reference`;
and the merchant portal needs to list recent links so staff can copy one
into an order confirmation email.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.datetime_utils import ensure_aware_utc
from bims_shopify.domain.payment import PaymentIntent

from .models import PaymentIntentModel


def _to_domain(model: PaymentIntentModel) -> PaymentIntent:
    return PaymentIntent(
        tenant_slug="",
        order_id=model.order_id,
        amount=model.amount,
        currency=model.currency,
        checkout_url=model.checkout_url,
        provider_reference=model.provider_reference,
        status=model.status,
    )


class SqlAlchemyPaymentIntentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_order_id(
        self, tenant_id: int, provider: str, order_id: str
    ) -> PaymentIntent | None:
        result = await self._session.execute(
            select(PaymentIntentModel).where(
                PaymentIntentModel.tenant_id == tenant_id,
                PaymentIntentModel.provider == provider,
                PaymentIntentModel.order_id == order_id,
            )
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def get_by_provider_reference(
        self, tenant_id: int, provider: str, provider_reference: str
    ) -> PaymentIntent | None:
        result = await self._session.execute(
            select(PaymentIntentModel).where(
                PaymentIntentModel.tenant_id == tenant_id,
                PaymentIntentModel.provider == provider,
                PaymentIntentModel.provider_reference == provider_reference,
            )
        )
        model = result.scalar_one_or_none()
        return _to_domain(model) if model is not None else None

    async def save(self, tenant_id: int, provider: str, intent: PaymentIntent) -> None:
        """Upsert by (tenant_id, provider, order_id)."""
        result = await self._session.execute(
            select(PaymentIntentModel).where(
                PaymentIntentModel.tenant_id == tenant_id,
                PaymentIntentModel.provider == provider,
                PaymentIntentModel.order_id == intent.order_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            model = PaymentIntentModel(tenant_id=tenant_id, provider=provider, order_id=intent.order_id)
            self._session.add(model)
        model.amount = intent.amount
        model.currency = intent.currency
        model.status = intent.status
        model.checkout_url = intent.checkout_url
        model.provider_reference = intent.provider_reference
        await self._session.commit()

    async def update_status(
        self, tenant_id: int, provider: str, order_id: str, status: str
    ) -> None:
        result = await self._session.execute(
            select(PaymentIntentModel).where(
                PaymentIntentModel.tenant_id == tenant_id,
                PaymentIntentModel.provider == provider,
                PaymentIntentModel.order_id == order_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is not None:
            model.status = status
            await self._session.commit()

    async def list_recent(self, tenant_id: int, limit: int = 50) -> list[dict]:
        result = await self._session.execute(
            select(PaymentIntentModel)
            .where(PaymentIntentModel.tenant_id == tenant_id)
            .order_by(PaymentIntentModel.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        models = result.scalars().all()
        out = []
        for model in models:
            created_at = ensure_aware_utc(model.created_at)
            updated_at = ensure_aware_utc(model.updated_at)
            out.append(
                {
                    "order_id": model.order_id,
                    "provider": model.provider,
                    "amount": model.amount,
                    "currency": model.currency,
                    "status": model.status,
                    "checkout_url": model.checkout_url,
                    "provider_reference": model.provider_reference,
                    "created_at": created_at.isoformat() if created_at else None,
                    "updated_at": updated_at.isoformat() if updated_at else None,
                }
            )
        return out
