"""Background runner for long catalog/rekey ops commands, triggered via the admin API.

Replaces SSH+CLI as the only way to run `catalog.py`/`rekey_skus.py`: a job
row is created in `ops_jobs`, an asyncio task is launched to do the actual
work, and progress/result/error are persisted to that row so an admin can
poll it instead of tailing a terminal.

Single-flight is enforced per (tenant_id, command) via an asyncio.Lock keyed
on that pair; a second request for the same tenant+command while one is
already running gets a 409 (see api/ops.py) instead of silently overlapping
with wipes/imports.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.models import OpsJobModel
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.config import Settings
from bims_shopify.logging import get_logger
from bims_shopify.ops import catalog, rekey_skus, sync_job

logger = get_logger(__name__)

#: Throttle for progress-callback DB writes: at most one UPDATE per tenant
#: this often, regardless of how many progress ticks fire in between.
PROGRESS_WRITE_INTERVAL_SECONDS = 2.0

_COMMAND_RUNNERS = {
    "status": catalog.run_status,
    "wipe": catalog.run_wipe,
    "import": catalog.run_import,
    "dedupe": catalog.run_dedupe,
    "rekey": rekey_skus.run_rekey,
    "fix_tracking": catalog.run_fix_tracking,
    "cleanup_no_stock": catalog.run_cleanup_no_stock,
    "sync": sync_job.run_sync,
}


class JobConflictError(Exception):
    """Raised when a job for the same (tenant, command) is already running."""


class JobRunner:
    def __init__(self, session_factory: async_sessionmaker, settings: Settings) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        # Strong references to in-flight tasks so they are not garbage
        # collected mid-run (asyncio only holds a weak ref once created).
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Optional: shared per-tenant sync lock provider (set by main.py to
        # `TenantSyncScheduler.lock_for_tenant`). When set, the "sync"
        # command additionally acquires this lock so a sync job can never
        # run concurrently with the APScheduler tick or a manual HTTP sync
        # (`POST /sync/{slug}/run`) for the same tenant -- all three share
        # the exact same lock object. See `set_sync_lock_provider`.
        self._tenant_sync_lock_provider: Callable[[int], asyncio.Lock] | None = None

    def set_sync_lock_provider(self, provider: Callable[[int], asyncio.Lock]) -> None:
        self._tenant_sync_lock_provider = provider

    def _lock_for(self, tenant_id: int, command: str) -> asyncio.Lock:
        key = (tenant_id, command)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def is_running(self, tenant_id: int, command: str) -> bool:
        return self._lock_for(tenant_id, command).locked()

    async def start_job(self, tenant_id: int, command: str, options: dict[str, Any]) -> OpsJobModel:
        if self.is_running(tenant_id, command):
            raise JobConflictError(f"a '{command}' job is already running for this tenant")

        async with self._session_factory() as session:
            job = OpsJobModel(
                tenant_id=tenant_id, command=command, options=options, status="queued"
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)

        task = asyncio.create_task(self._run(job.id, tenant_id, command, options))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return job

    async def _run(self, job_id: int, tenant_id: int, command: str, options: dict[str, Any]) -> None:
        lock = self._lock_for(tenant_id, command)
        async with lock:
            if command == "sync" and self._tenant_sync_lock_provider is not None:
                # Also hold the shared tenant sync lock for the whole run,
                # so this job serializes with the scheduler tick and manual
                # HTTP sync -- not just with other "sync" ops jobs.
                shared_lock = self._tenant_sync_lock_provider(tenant_id)
                async with shared_lock:
                    await self._execute(job_id, tenant_id, command, options)
            else:
                await self._execute(job_id, tenant_id, command, options)

    async def _execute(self, job_id: int, tenant_id: int, command: str, options: dict[str, Any]) -> None:
        async with self._session_factory() as session:
            tenant_repo = SqlAlchemyTenantRepository(session, SecretBox(self._settings.fernet_key))
            tenant = await tenant_repo.get_by_id(tenant_id)
            if tenant is None:
                await self._mark_finished(job_id, status="failed", error="tenant not found")
                return

            audit = SqlAlchemyAuditLogger(session)
            await audit.log(
                actor="system",
                action="ops.job_started",
                entity="ops_job",
                tenant_id=tenant_id,
                payload={"job_id": job_id, "command": command, "options": options},
            )

        await self._mark_started(job_id)

        last_write = 0.0

        async def progress(done: int, total: int | None, message: str) -> None:
            nonlocal last_write
            now = time.monotonic()
            if now - last_write < PROGRESS_WRITE_INTERVAL_SECONDS:
                return
            last_write = now
            await self._update_progress(job_id, done, total, message)

        runner = _COMMAND_RUNNERS[command]
        try:
            result = await runner(tenant, options, progress)
        except SystemExit as exc:
            await self._mark_finished(job_id, status="failed", error=str(exc))
            await self._audit_finished(tenant_id, job_id, command, "failed")
        except Exception as exc:  # a job failure must not crash the process
            logger.error("ops_job_failed", job_id=job_id, command=command, error=str(exc))
            await self._mark_finished(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            await self._audit_finished(tenant_id, job_id, command, "failed")
        else:
            await self._mark_finished(job_id, status="succeeded", result=result)
            await self._audit_finished(tenant_id, job_id, command, "succeeded", result=result)

    async def _audit_finished(
        self, tenant_id: int, job_id: int, command: str, status: str, result: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"job_id": job_id, "command": command, "status": status}
        if result:
            # Key counts only -- never dump full result payloads into audit_logs.
            for key in ("products", "variants", "deleted", "created"):
                if key in result:
                    payload[key] = result[key]
        async with self._session_factory() as session:
            audit = SqlAlchemyAuditLogger(session)
            await audit.log(
                actor="system", action="ops.job_finished", entity="ops_job", tenant_id=tenant_id, payload=payload
            )

    async def _mark_started(self, job_id: int) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(OpsJobModel)
                .where(OpsJobModel.id == job_id)
                .values(status="running", started_at=datetime.now(UTC))
            )
            await session.commit()

    async def _update_progress(self, job_id: int, done: int, total: int | None, message: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(OpsJobModel)
                .where(OpsJobModel.id == job_id)
                .values(progress_done=done, progress_total=total, message=message)
            )
            await session.commit()

    async def _mark_finished(
        self,
        job_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(OpsJobModel)
                .where(OpsJobModel.id == job_id)
                .values(status=status, result=result, error=error, finished_at=datetime.now(UTC))
            )
            await session.commit()

    @staticmethod
    async def mark_interrupted_on_startup(session_factory: async_sessionmaker) -> int:
        """Mark any `running` job as `interrupted` (container restarted mid-job).

        Called once from the FastAPI lifespan, before the scheduler starts.
        Returns the number of rows affected (used only for logging).
        """
        async with session_factory() as session:
            result = await session.execute(
                select(OpsJobModel.id).where(OpsJobModel.status == "running")
            )
            ids = [row[0] for row in result.all()]
            if ids:
                await session.execute(
                    update(OpsJobModel)
                    .where(OpsJobModel.id.in_(ids))
                    .values(status="interrupted", finished_at=datetime.now(UTC))
                )
                await session.commit()
            return len(ids)
