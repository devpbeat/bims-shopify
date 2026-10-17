"""The application lifespan wires BOTH workers into one scheduler."""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bims_shopify import main
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.config import Settings
from bims_shopify.domain.tenant import Tenant


async def test_lifespan_schedules_a_pull_job_and_a_push_job_per_active_tenant(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        pass
    settings = Settings(
        admin_token="t",
        fernet_key=Fernet.generate_key().decode("utf-8"),
        database_url=f"sqlite+aiosqlite:///{db_file.name}",
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()

    engine = create_async_engine(settings.database_url, future=True)
    try:
        async with app.router.lifespan_context(app):
            scheduler = app.state.scheduler
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key)).create(
                    Tenant(
                        id=None, slug="acme", bims_base_url="https://bims.example.com",
                        bims_api_key="k", shopify_shop_domain="acme.myshopify.com",
                        shopify_access_token="t", shopify_webhook_secret="w",
                        shopify_location_id="loc", bims_posale_id=1, bims_warehouse_id=1,
                        bims_company_id=1, bims_currency_id=1, bims_payment_method_id=1,
                        default_customer_contact_id=1, sync_interval_minutes=30,
                        push_interval_minutes=5,
                    )
                )
            await scheduler.reconcile()

            pull = scheduler._scheduler.get_job("tenant-pull-1")
            push = scheduler._scheduler.get_job("tenant-push-1")
            assert pull.trigger.interval == timedelta(minutes=30)
            assert push.trigger.interval == timedelta(minutes=5)
            reconcile = scheduler._scheduler.get_job("tenant-reconcile-1")
            assert reconcile.trigger.interval == timedelta(minutes=480)
            assert scheduler._last_reconcile_fn is not None
            # the on-demand reconcile job shares the reconcile + push locks
            assert app.state.job_runner._tenant_reconcile_lock_provider(1).locked() is False
            # skip recording and boot-time pull timing are wired, not left at None
            assert scheduler._skip_fn is not None and scheduler._last_pull_fn is not None
            # the ops "sync" job shares the combined pull+push lock
            assert app.state.job_runner._tenant_sync_lock_provider(1).locked() is False
    finally:
        await engine.dispose()
        os.unlink(db_file.name)
