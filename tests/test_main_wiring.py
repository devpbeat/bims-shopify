"""The application lifespan wires the pull loop and the write cycle into one scheduler."""
from __future__ import annotations

import asyncio
import os
import tempfile

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify import main
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import Tenant


async def test_lifespan_runs_one_continuous_pull_loop_per_active_tenant(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="t",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    pulled: list[str] = []
    cycled: list[str] = []

    async def fake_pull(tenant, session, **kwargs):
        pulled.append(tenant.slug)
        return {"status": "ok"}

    async def fake_cycle(tenant, session, **kwargs):
        cycled.append(tenant.slug)

    monkeypatch.setattr(main, "do_pull", fake_pull)
    monkeypatch.setattr(main, "do_cycle", fake_cycle)
    app = main.create_app()

    engine = create_async_engine(settings.database_url, future=True)
    try:
        async with app.router.lifespan_context(app):
            scheduler = app.state.scheduler
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                created = await SqlAlchemyTenantRepository(
                    session, SecretBox(settings.fernet_key)
                ).create(
                    Tenant(
                        id=None, slug="acme", bims_base_url="https://bims.example.com",
                        bims_api_key="k", shopify_shop_domain="acme.myshopify.com",
                        shopify_access_token="t", shopify_webhook_secret="w",
                        shopify_location_id="loc", bims_posale_id=1, bims_warehouse_id=1,
                        bims_company_id=1, bims_currency_id=1, bims_payment_method_id=1,
                        default_customer_contact_id=1, pull_min_gap_seconds=3600,
                    )
                )
            await scheduler.align()

            # One loop per active tenant, no per-worker interval jobs any more.
            assert set(scheduler._loops) == {created.id}
            assert {job.id for job in scheduler._scheduler.get_jobs()} == {"tenant-sync-align"}
            # The loop pulled at once (never pulled before), and the snapshot that pull
            # landed triggered exactly one write cycle.
            for _ in range(200):
                if cycled:
                    break
                await asyncio.sleep(0.01)
            assert pulled == ["acme"] and cycled == ["acme"]
            # skip recording and boot-time pull timing are wired, not left at None
            assert scheduler._skip_fn is not None and scheduler._last_pull_fn is not None
            assert scheduler._cycle_fn is not None
            # the on-demand reconcile job holds the scheduler's write lock...
            assert app.state.job_runner._tenant_reconcile_lock_provider(1) is scheduler.locks.write(1)
            # ...and the ops "sync" job the combined pull+write lock
            assert app.state.job_runner._tenant_sync_lock_provider(1).locked() is False
    finally:
        await engine.dispose()
        os.unlink(db_file.name)
