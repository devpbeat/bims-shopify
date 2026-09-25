"""Scheduler coordinator that auto-enqueues catalog imports for tenants opted in.

This is a *separate* ticking loop from `TenantSyncScheduler` (which keeps
Shopify stock in sync from BIMS sales every `sync_interval_minutes`). This
coordinator's only job is to notice new BIMS products and get them into
Shopify without a manual `catalog import` run:

For each active tenant with `auto_import_products=True`, once per tick
(`AUTO_IMPORT_TICK_MINUTES`) we check whether it is "due" -- no prior
scheduler-triggered import has ever finished for it, or the last one
finished more than `auto_import_interval_minutes` ago -- and, if so and no
import job is currently queued/running for that tenant, enqueue one via
`JobRunner` with `command="import"`, `apply=True`, and
`publish=tenant.auto_import_publish` and
`only_with_stock=tenant.auto_import_only_with_stock`.

Import-only, on purpose: this coordinator never enqueues `wipe` or `dedupe`.
Those stay manual/admin-triggered because they can delete or merge data.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bims_shopify.adapters.persistence.models import OpsJobModel
from bims_shopify.datetime_utils import ensure_aware_utc
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger
from bims_shopify.ops.job_runner import JobConflictError, JobRunner
from bims_shopify.ports.repositories import TenantRepository

logger = get_logger(__name__)

#: Marker stored in ops_jobs.options so scheduler-triggered imports can be
#: told apart from manually-triggered ones when computing "last finished".
AUTO_IMPORT_TRIGGER = "scheduler"

#: How many of a tenant's most recent finished import jobs to scan looking
#: for the last scheduler-triggered one. Small and bounded -- we don't need
#: full history, just the most recent match.
_RECENT_IMPORT_JOBS_LOOKBACK = 20

#: Re-pull products modified within this window before the last successful
#: scheduled import, to tolerate clock skew / near-miss writes on the BIMS
#: side. Mirrors INCREMENTAL_OVERLAP in api/sync.py.
INCREMENTAL_OVERLAP = timedelta(minutes=15)


class AutoImportCoordinator:
    """Iterates active, opted-in tenants on an interval and enqueues a
    catalog import via JobRunner when one is due and none is already
    queued/running for that tenant."""

    def __init__(
        self,
        tenant_repository: TenantRepository,
        job_runner: JobRunner,
        session_factory: async_sessionmaker,
        tick_minutes: int = 15,
    ) -> None:
        self._tenant_repository = tenant_repository
        self._job_runner = job_runner
        self._session_factory = session_factory
        self._tick_minutes = tick_minutes

    @property
    def tick_minutes(self) -> int:
        return self._tick_minutes

    async def run_once(self) -> None:
        tenants = await self._tenant_repository.list_active()
        for tenant in tenants:
            if tenant.id is None or not tenant.active or not tenant.auto_import_products:
                continue
            try:
                await self._maybe_enqueue(tenant)
            except Exception as exc:  # one tenant's failure must not affect others
                logger.error(
                    "auto_import_tenant_failed",
                    tenant_id=tenant.id,
                    tenant_slug=tenant.slug,
                    error=str(exc),
                )

    async def _maybe_enqueue(self, tenant: Tenant) -> None:
        assert tenant.id is not None
        if await self._has_pending_import(tenant.id):
            logger.info("auto_import_skipped_job_in_flight", tenant_id=tenant.id, tenant_slug=tenant.slug)
            return

        last_finished_at = await self._last_scheduler_import_finished_at(tenant.id)
        if not self._is_due(tenant, last_finished_at):
            logger.info("auto_import_skipped_not_due", tenant_id=tenant.id, tenant_slug=tenant.slug)
            return

        # First-ever scheduled import for this tenant runs full (no prior
        # watermark to trust); every subsequent one is incremental, pulling
        # only products BIMS reports as created/modified since the last
        # successful scheduled import (minus a small overlap for clock skew).
        since_iso = (
            (last_finished_at - INCREMENTAL_OVERLAP).isoformat()
            if last_finished_at is not None
            else None
        )

        try:
            await self._job_runner.start_job(
                tenant.id,
                "import",
                {
                    "apply": True,
                    "publish": tenant.auto_import_publish,
                    "only_with_stock": tenant.auto_import_only_with_stock,
                    "since_iso": since_iso,
                    "_trigger": AUTO_IMPORT_TRIGGER,
                },
            )
            logger.info(
                "auto_import_enqueued",
                tenant_id=tenant.id,
                tenant_slug=tenant.slug,
                incremental=since_iso is not None,
            )
        except JobConflictError:
            # A job started between our _has_pending_import check and here.
            logger.info("auto_import_skipped_conflict", tenant_id=tenant.id, tenant_slug=tenant.slug)

    async def _has_pending_import(self, tenant_id: int) -> bool:
        if self._job_runner.is_running(tenant_id, "import"):
            return True
        async with self._session_factory() as session:
            result = await session.execute(
                select(OpsJobModel.id).where(
                    OpsJobModel.tenant_id == tenant_id,
                    OpsJobModel.command == "import",
                    OpsJobModel.status.in_(["queued", "running"]),
                )
            )
            return result.scalar_one_or_none() is not None

    def _is_due(self, tenant: Tenant, last_finished_at: datetime | None) -> bool:
        if last_finished_at is None:
            return True
        elapsed_minutes = (datetime.now(UTC) - last_finished_at).total_seconds() / 60
        return elapsed_minutes >= tenant.auto_import_interval_minutes

    async def _last_scheduler_import_finished_at(self, tenant_id: int) -> datetime | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(OpsJobModel.options, OpsJobModel.created_at)
                .where(
                    OpsJobModel.tenant_id == tenant_id,
                    OpsJobModel.command == "import",
                    OpsJobModel.finished_at.is_not(None),
                )
                .order_by(OpsJobModel.created_at.desc())
                .limit(_RECENT_IMPORT_JOBS_LOOKBACK)
            )
            for options, created_at in result.all():
                if (options or {}).get("_trigger") == AUTO_IMPORT_TRIGGER:
                    return ensure_aware_utc(created_at)
        return None
