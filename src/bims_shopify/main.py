"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import functools
import os
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import FileResponse

from alembic import command
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.database import create_engine_and_sessionmaker
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import (
    audit,
    health,
    ops,
    payments,
    portal,
    shopify_oauth,
    sync,
    tenants,
    webhooks,
)
from bims_shopify.config import get_settings
from bims_shopify.logging import configure_logging, get_logger
from bims_shopify.ops.auto_import import AutoImportCoordinator
from bims_shopify.ops.job_runner import JobRunner
from bims_shopify.ops.sync_workers import do_pull, do_push, last_pull_at, record_skip
from bims_shopify.scheduler import TenantSyncScheduler

logger = get_logger(__name__)

def _find_repo_root() -> Path:
    """Locate the directory containing alembic.ini.

    In local dev (`uv run`), the package is imported from `src/`, two
    parents up from this file. In the Docker image, the package is `pip`
    installed into site-packages, so `__file__` is nowhere near the repo
    checkout — but `alembic.ini`/`alembic/` are COPYed into the container's
    WORKDIR (`/app`), which is also the process's cwd. Try the source-tree
    relative path first (dev), then fall back to cwd (container/production).
    """
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "alembic.ini").exists():
        return candidate
    return Path.cwd()


_REPO_ROOT = _find_repo_root()
_FRONTEND_DIST = (_REPO_ROOT / "frontend" / "dist").resolve()


def _run_migrations(database_url: str) -> None:
    """Run `alembic upgrade head` synchronously against the given DB URL.

    Alembic's command API is synchronous. `env.py` builds its own async
    engine internally (see ALEMBIC_DATABASE_URL / Settings resolution), so
    all we need to do here is point alembic at the right URL and invoke it.
    This replaces `Base.metadata.create_all`, which silently ignored schema
    drift (e.g. new columns added to models but never applied to an
    existing running database) and caused production 500s.
    """
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = database_url
    try:
        command.upgrade(cfg, "head")
        command.current(cfg, verbose=False)
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)
    logger.info("database_migrated")


def _make_pull_fn(session_factory):
    """Build the scheduler's per-tenant PULL callback (BIMS -> snapshot).

    The same ``do_pull`` the manual one-shot runs, so a scheduled pull and a
    manual one never diverge: the sanity guard, the atomic snapshot write, the
    catalog stats and the outcome bookkeeping are all inside it.
    """

    async def _pull_tenant(tenant) -> None:
        async with session_factory() as session:
            try:
                await do_pull(tenant, session, trigger="scheduled")
            except Exception as exc:  # one tenant's failure must not affect others
                logger.error(
                    "scheduled_pull_tenant_failed",
                    tenant_slug=tenant.slug,
                    tenant_id=tenant.id,
                    error=str(exc),
                )

    return _pull_tenant


def _make_push_fn(session_factory):
    """Build the scheduler's per-tenant PUSH callback (snapshot -> Shopify).

    The same ``do_push`` the manual one-shot runs. It never calls BIMS and
    declines, with a recorded reason, when the snapshot is missing,
    incomplete or older than the tenant's staleness limit.
    """

    async def _push_tenant(tenant) -> None:
        async with session_factory() as session:
            try:
                await do_push(tenant, session, trigger="scheduled")
            except Exception as exc:  # one tenant's failure must not affect others
                logger.error(
                    "scheduled_push_tenant_failed",
                    tenant_slug=tenant.slug,
                    tenant_id=tenant.id,
                    error=str(exc),
                )

    return _push_tenant


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine, session_factory = create_engine_and_sessionmaker(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(_run_migrations, settings.database_url)

        interrupted_count = await JobRunner.mark_interrupted_on_startup(session_factory)
        if interrupted_count:
            logger.warning("ops_jobs_marked_interrupted", count=interrupted_count)
        job_runner = JobRunner(session_factory, settings)
        app.state.job_runner = job_runner

        # A fresh session per scheduler lookup (never one long-lived session):
        # the scheduler must see interval/mode/active changes made through the
        # API, which a session-scoped identity map would hide forever.
        @asynccontextmanager
        async def tenant_repository_scope():
            async with session_factory() as session:
                yield SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))

        async with tenant_repository_scope() as repo:
            scheduler = TenantSyncScheduler(
                repo,
                _make_pull_fn(session_factory),
                repository_scope=tenant_repository_scope,
                push_fn=_make_push_fn(session_factory),
                skip_fn=functools.partial(record_skip, session_factory),
                last_pull_fn=functools.partial(last_pull_at, session_factory),
            )
        app.state.scheduler = scheduler
        # Share this scheduler's per-tenant locks with the "sync" ops job
        # (see JobRunner._run): the combined pull+push lock, so a background
        # sync job, the scheduled pull and push ticks, and a manual HTTP sync
        # can never overlap for the same tenant.
        job_runner.set_sync_lock_provider(scheduler.lock_for_tenant)
        scheduler.start()

        # Separate ticking loop from the inventory-sync scheduler above: this
        # one only enqueues catalog imports for tenants opted into
        # auto-import (see ops/auto_import.py), never touches inventory sync.
        async with session_factory() as session:
            auto_import_repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
            auto_import_coordinator = AutoImportCoordinator(
                auto_import_repo,
                job_runner,
                session_factory,
                settings.auto_import_tick_minutes,
            )
        app.state.auto_import_coordinator = auto_import_coordinator
        auto_import_scheduler = AsyncIOScheduler()
        auto_import_scheduler.add_job(
            auto_import_coordinator.run_once,
            trigger=IntervalTrigger(minutes=settings.auto_import_tick_minutes),
            id="auto-import-coordinator",
            replace_existing=True,
        )
        auto_import_scheduler.start()

        try:
            yield
        finally:
            scheduler.shutdown()
            auto_import_scheduler.shutdown(wait=False)
            await engine.dispose()

    app = FastAPI(title="bims-shopify", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine

    app.include_router(health.router)
    app.include_router(tenants.router)
    app.include_router(webhooks.router)
    app.include_router(payments.router)
    app.include_router(sync.router)
    app.include_router(shopify_oauth.router)
    app.include_router(portal.router)
    app.include_router(audit.router)
    app.include_router(ops.router)
    _mount_portal_spa(app)
    return app


def _mount_portal_spa(app: FastAPI) -> None:
    """Serve the built merchant portal (`frontend/dist`) with client-side routing.

    The SPA owns every `/portal` and `/portal/{path}` URL: known built assets
    (JS/CSS/favicon under `dist/`) are served as-is, and anything else falls
    back to `index.html` so React Router can render `/portal/{slug}` without
    a matching filesystem path. This is a plain route rather than
    `StaticFiles` mounted at `/portal` because a mount would 404 on unknown
    paths (e.g. `/portal/mystore`) instead of falling back to the SPA shell.
    """
    index_path = _FRONTEND_DIST / "index.html"

    @app.get("/portal")
    async def portal_root() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/portal/{full_path:path}")
    async def portal_spa(full_path: str) -> FileResponse:
        candidate = (_FRONTEND_DIST / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(_FRONTEND_DIST):
            return FileResponse(candidate)
        return FileResponse(index_path)


app = create_app()
