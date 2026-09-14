"""SQLAlchemy-backed repository for rekey reports and portal resolutions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import RekeyReportModel, RekeyResolutionModel


class SqlAlchemyRekeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest_report(self, tenant_id: int) -> RekeyReportModel | None:
        result = await self._session.execute(
            select(RekeyReportModel)
            .where(RekeyReportModel.tenant_id == tenant_id)
            .order_by(RekeyReportModel.created_at.desc(), RekeyReportModel.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_resolutions_for_report(self, report_id: int) -> list[RekeyResolutionModel]:
        result = await self._session.execute(
            select(RekeyResolutionModel).where(RekeyResolutionModel.report_id == report_id)
        )
        return list(result.scalars().all())

    async def get_resolution(self, report_id: int, variant_id: str) -> RekeyResolutionModel | None:
        result = await self._session.execute(
            select(RekeyResolutionModel).where(
                RekeyResolutionModel.report_id == report_id,
                RekeyResolutionModel.variant_id == variant_id,
            )
        )
        return result.scalar_one_or_none()

    async def record_resolution(
        self,
        *,
        tenant_id: int,
        report_id: int,
        variant_id: str,
        action: str,
        status: str,
        error: str | None = None,
        note: str | None = None,
    ) -> RekeyResolutionModel:
        """Insert or update the resolution row for ``(report_id, variant_id)``.

        Only ever called for a variant with no existing resolution, or one
        whose previous attempt failed (see the portal API's idempotency
        check) — so this always represents a fresh apply attempt, not a
        second decision overwriting a settled one.
        """
        existing = await self.get_resolution(report_id, variant_id)
        if existing is not None:
            existing.action = action
            existing.status = status
            existing.error = error
            existing.note = note
            await self._session.commit()
            await self._session.refresh(existing)
            return existing

        model = RekeyResolutionModel(
            tenant_id=tenant_id,
            report_id=report_id,
            variant_id=variant_id,
            action=action,
            status=status,
            error=error,
            note=note,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model
