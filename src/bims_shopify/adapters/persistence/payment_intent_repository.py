"""SQLAlchemy-backed repository for persisted hosted-checkout payment intents.

Persisting the intent (rather than only relying on `processed_events` for
idempotency) exists for two reasons: Pagopar's callback never echoes the
merchant's own Shopify order id (only its internal `hash_pedido`), so the
callback must resolve `order_id` from a row keyed by `provider_reference`;
and the merchant portal needs to list recent links so staff can copy one
into an order confirmation email.
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.datetime_utils import ensure_aware_utc
from bims_shopify.domain.payment import PaymentIntent
from bims_shopify.domain.payment_report import EXPORT_ROW_CAP, ReportFilters

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
        process_id=model.process_id,
        row_id=model.id,
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
        model.process_id = intent.process_id
        await self._session.commit()
        intent.row_id = model.id

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

    @staticmethod
    def _report_conditions(tenant_id: int, filters: ReportFilters) -> list:
        conditions = [PaymentIntentModel.tenant_id == tenant_id]
        if filters.date_from is not None:
            start = datetime.combine(filters.date_from, time.min, tzinfo=UTC)
            conditions.append(PaymentIntentModel.created_at >= start)
        if filters.date_to is not None:
            end = datetime.combine(filters.date_to + timedelta(days=1), time.min, tzinfo=UTC)
            conditions.append(PaymentIntentModel.created_at < end)
        if filters.status:
            conditions.append(PaymentIntentModel.status == filters.status)
        if filters.provider:
            conditions.append(PaymentIntentModel.provider == filters.provider)
        return conditions

    async def report_rows(
        self, tenant_id: int, filters: ReportFilters, limit: int, offset: int
    ) -> list[dict]:
        """Newest-first page of intents; ``checkout_url`` is deliberately omitted."""
        result = await self._session.execute(
            select(PaymentIntentModel)
            .where(*self._report_conditions(tenant_id, filters))
            .order_by(PaymentIntentModel.created_at.desc(), PaymentIntentModel.id.desc())
            .limit(max(1, min(limit, EXPORT_ROW_CAP)))
            .offset(max(0, offset))
        )
        out = []
        for model in result.scalars().all():
            created_at = ensure_aware_utc(model.created_at)
            updated_at = ensure_aware_utc(model.updated_at)
            out.append(
                {
                    "id": model.id,
                    "order_id": model.order_id,
                    "provider": model.provider,
                    "amount": model.amount,
                    "currency": model.currency,
                    "status": model.status,
                    "provider_reference": model.provider_reference,
                    "process_id": model.process_id,
                    "created_at": created_at.isoformat() if created_at else None,
                    "updated_at": updated_at.isoformat() if updated_at else None,
                }
            )
        return out

    async def report_aggregates(self, tenant_id: int, filters: ReportFilters) -> dict:
        """Count and total amount per status and per UTC day, over ALL matching rows."""
        conditions = self._report_conditions(tenant_id, filters)
        total = func.coalesce(func.sum(PaymentIntentModel.amount), 0.0)
        by_status = await self._session.execute(
            select(PaymentIntentModel.status, func.count(), total)
            .where(*conditions)
            .group_by(PaymentIntentModel.status)
            .order_by(PaymentIntentModel.status)
        )
        day = func.date(PaymentIntentModel.created_at)
        by_day = await self._session.execute(
            select(day, func.count(), total).where(*conditions).group_by(day).order_by(day)
        )
        return {
            "by_status": [
                {"status": s, "count": c, "total_amount": t} for s, c, t in by_status.all()
            ],
            "by_day": [{"date": str(d), "count": c, "total_amount": t} for d, c, t in by_day.all()],
        }

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
