"""SQLAlchemy adapter for the operator payment purge.

Every statement is filtered by ``tenant_id`` and ``provider``/event sources, and
the tenant is read through selected columns only, so no secret is ever loaded.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.application.payment_purge import PurgePlan, PurgeScope, PurgeTenant
from bims_shopify.datetime_utils import ensure_aware_utc

from .models import PaymentIntentModel, ProcessedEventModel, TenantModel

SAMPLE_SIZE = 5


def _intent_conditions(scope: PurgeScope) -> list:
    conditions = [
        PaymentIntentModel.tenant_id == scope.tenant_id,
        PaymentIntentModel.provider == scope.provider,
    ]
    if scope.created_after is not None:
        conditions.append(PaymentIntentModel.created_at >= scope.created_after)
    if scope.created_before is not None:
        conditions.append(PaymentIntentModel.created_at < scope.created_before)
    return conditions


def _event_conditions(scope: PurgeScope) -> list:
    conditions = [
        ProcessedEventModel.tenant_id == scope.tenant_id,
        ProcessedEventModel.source.in_(scope.event_sources),
    ]
    if scope.created_after is not None:
        conditions.append(ProcessedEventModel.created_at >= scope.created_after)
    if scope.created_before is not None:
        conditions.append(ProcessedEventModel.created_at < scope.created_before)
    return conditions


class SqlAlchemyPaymentPurgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_tenant(self, slug: str) -> PurgeTenant | None:
        row = (
            await self._session.execute(
                select(TenantModel.id, TenantModel.slug, TenantModel.provider_config).where(
                    TenantModel.slug == slug
                )
            )
        ).first()
        if row is None:
            return None
        return PurgeTenant(row.id, row.slug, (row.provider_config or {}).get("environment"))

    async def plan(self, scope: PurgeScope) -> PurgePlan:
        where = _intent_conditions(scope)
        by_status = (
            await self._session.execute(
                select(PaymentIntentModel.status, func.count())
                .where(*where)
                .group_by(PaymentIntentModel.status)
            )
        ).all()
        sample = (
            await self._session.scalars(
                select(PaymentIntentModel)
                .where(*where)
                .order_by(PaymentIntentModel.created_at, PaymentIntentModel.id)
                .limit(SAMPLE_SIZE)
            )
        ).all()
        bounds = (
            await self._session.execute(
                select(func.min(PaymentIntentModel.created_at), func.max(PaymentIntentModel.created_at))
                .where(*where)
            )
        ).one()
        events = (
            await self._session.execute(
                select(func.count()).select_from(ProcessedEventModel).where(*_event_conditions(scope))
            )
        ).scalar_one()

        def iso(value):
            aware = ensure_aware_utc(value)
            return aware.isoformat() if aware else None

        return PurgePlan(
            intents=sum(count for _, count in by_status),
            events=events,
            by_status={status: count for status, count in by_status},
            sample=[
                {
                    "order_id": m.order_id,
                    "status": m.status,
                    "amount": m.amount,
                    "currency": m.currency,
                    "created_at": iso(m.created_at),
                }
                for m in sample
            ],
            oldest=iso(bounds[0]),
            newest=iso(bounds[1]),
        )

    async def delete(self, scope: PurgeScope) -> tuple[int, int]:
        events = await self._session.execute(
            delete(ProcessedEventModel).where(*_event_conditions(scope))
        )
        intents = await self._session.execute(
            delete(PaymentIntentModel).where(*_intent_conditions(scope))
        )
        await self._session.commit()
        return intents.rowcount or 0, events.rowcount or 0
