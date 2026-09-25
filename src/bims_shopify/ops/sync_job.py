"""In-process entry point for running inventory sync as a background ops job.

A full/force sync pulls the entire BIMS catalog (tens of thousands of
products) plus stock for every SKU, which can take several minutes. Running
that synchronously inside `POST /sync/{slug}/run` makes Traefik time out the
HTTP request and return a 502, even though the sync itself may have
succeeded. Wrapping the same sync logic as an ops job (`POST /ops/{slug}/run`
with `command: "sync"`) lets it run in the background: the HTTP request
returns immediately with a job_id, and progress/result/error are polled via
`GET /ops/{slug}/jobs/{job_id}`, exactly like `import`/`wipe`/`dedupe`.

This module wraps `bims_shopify.api.sync._do_run_sync` -- the exact same
coroutine used by the manual HTTP endpoint (kept for the APScheduler tick /
back-compat) -- so sync behavior never diverges between the two entry
points.
"""
from __future__ import annotations

from typing import Any

from bims_shopify.adapters.persistence.database import create_engine_and_sessionmaker
from bims_shopify.api.sync import _do_run_sync
from bims_shopify.application.sync_inventory import ProgressFn
from bims_shopify.config import get_settings
from bims_shopify.domain.tenant import Tenant


async def run_sync(
    tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None
) -> dict[str, Any]:
    """Run inventory sync in-process, for the JobRunner.

    Options: ``dry_run`` (default False), ``full`` (default False), ``force``
    (default False) -- same semantics as the HTTP endpoint's query params.

    Concurrency: the caller (JobRunner._run) is responsible for acquiring the
    tenant's shared sync lock (the same `asyncio.Lock` object returned by
    `TenantSyncScheduler.lock_for_tenant`, also used by the manual HTTP
    endpoint) before invoking this function, so a sync job can never overlap
    the scheduled sync or a concurrent manual HTTP run for the same tenant.
    This function does not acquire any lock itself.
    """
    dry_run = bool(options.get("dry_run", False))
    full = bool(options.get("full", False))
    force = bool(options.get("force", False))

    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with session_factory() as session:
            return await _do_run_sync(
                tenant,
                dry_run,
                session,
                full=full,
                force=force,
                progress=progress,
            )
    finally:
        await engine.dispose()
