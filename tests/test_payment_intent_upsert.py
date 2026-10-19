"""Regression: a new attempt overwrites the intent row for the same order."""
from __future__ import annotations

import sqlalchemy as sa

from bims_shopify.adapters.persistence.models import PaymentIntentModel
from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.domain.payment import PaymentIntent


async def test_save_upserts_on_tenant_provider_order_and_overwrites_reference(migrated_db):
    factory, make_tenant = migrated_db
    tenant = await make_tenant()

    def intent(reference: str, status: str) -> PaymentIntent:
        return PaymentIntent(
            tenant_slug=tenant.slug,
            order_id="1000000000001",
            amount=130.0,
            currency="PYG",
            checkout_url=f"https://pay.example.com/{reference}",
            provider_reference=reference,
            status=status,
        )

    async with factory() as session:
        repo = SqlAlchemyPaymentIntentRepository(session)
        await repo.save(tenant.id, "bancard_vpos", intent("100000000000100", "cancelled"))
        await repo.save(tenant.id, "bancard_vpos", intent("100000000000101", "pending"))

    async with factory() as session:
        rows = (await session.execute(sa.select(PaymentIntentModel))).scalars().all()
        assert len(rows) == 1
        assert rows[0].provider_reference == "100000000000101"
        assert rows[0].status == "pending"
        loaded = await SqlAlchemyPaymentIntentRepository(session).get_by_order_id(
            tenant.id, "bancard_vpos", "1000000000001"
        )
        assert loaded.provider_reference == "100000000000101"
