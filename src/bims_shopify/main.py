"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config
from fastapi import FastAPI

from alembic import command
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.database import create_engine_and_sessionmaker
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.api import health, payments, shopify_oauth, sync, tenants, webhooks
from bims_shopify.config import get_settings
from bims_shopify.logging import configure_logging, get_logger
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


def _make_sync_tenant_fn(session_factory):
    """Build the scheduler's per-tenant sync callback.

    Reuses the exact same `_do_run_sync` codepath as the manual
    `/sync/{slug}/run` endpoint (state persistence, incremental `since`,
    the per-run safety guard, deleted-products check, structured logging),
    so the scheduled job and manual runs never diverge in behavior. This
    matters in particular for `previous_stocks`/`last_run` bookkeeping:
    without it, every scheduled tick would see an empty previous-stock map,
    treat every SKU as "changed", and permanently trip the max-changed-
    variants safety guard for any catalog larger than that threshold.
    """

    async def _sync_tenant(tenant) -> None:
        from bims_shopify.api.sync import _do_run_sync

        async with session_factory() as session:
            try:
                await _do_run_sync(tenant, dry_run=False, session=session)
            except Exception as exc:  # one tenant's failure must not affect others
                logger.error(
                    "scheduled_sync_tenant_failed",
                    tenant_slug=tenant.slug,
                    tenant_id=tenant.id,
                    error=str(exc),
                )

    return _sync_tenant


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine, session_factory = create_engine_and_sessionmaker(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(_run_migrations, settings.database_url)

        async with session_factory() as session:
            repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
            scheduler = TenantSyncScheduler(
                repo, _make_sync_tenant_fn(session_factory), settings.sync_interval_minutes
            )
        app.state.scheduler = scheduler
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()
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
    return app


app = create_app()
