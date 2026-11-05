"""Tenant-scoped reads of ``ops_jobs`` for the portal."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from bims_shopify.adapters.persistence.models import OpsJobModel
from bims_shopify.domain.ops_job import OpsJobView


def _view(job: OpsJobModel, *, with_result: bool) -> OpsJobView:
    return OpsJobView(
        id=job.id,
        status=job.status,
        progress_done=job.progress_done or 0,
        progress_total=job.progress_total,
        message=job.message,
        created_at=job.created_at,
        finished_at=job.finished_at,
        result=job.result if with_result else None,
    )


class SqlAlchemyOpsJobReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest(self, tenant_id: int, command: str) -> OpsJobView | None:
        # The result column can be large; this lookup only needs the state.
        statement = (
            select(OpsJobModel)
            .options(defer(OpsJobModel.result))
            .where(OpsJobModel.tenant_id == tenant_id, OpsJobModel.command == command)
            .order_by(OpsJobModel.created_at.desc(), OpsJobModel.id.desc())
            .limit(1)
        )
        job = (await self._session.execute(statement)).scalar_one_or_none()
        return None if job is None else _view(job, with_result=False)

    async def latest_succeeded(self, tenant_id: int, command: str) -> OpsJobView | None:
        statement = (
            select(OpsJobModel)
            .where(
                OpsJobModel.tenant_id == tenant_id,
                OpsJobModel.command == command,
                OpsJobModel.status == "succeeded",
            )
            .order_by(OpsJobModel.created_at.desc(), OpsJobModel.id.desc())
            .limit(1)
        )
        job = (await self._session.execute(statement)).scalar_one_or_none()
        return None if job is None else _view(job, with_result=True)
