"""Staging payment-data purge: dry run by default, one tenant, audited, CLI only."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from bims_shopify.adapters.persistence.models import (
    AuditLogModel,
    PaymentIntentModel,
    ProcessedEventModel,
)
from bims_shopify.application.payment_purge import PurgeRefusedError, TenantNotFoundError
from bims_shopify.domain.tenant import PaymentProvider
from bims_shopify.ops.purge_payments import run_purge

OLD = datetime(2026, 10, 1, tzinfo=UTC)
NEW = datetime(2026, 11, 1, tzinfo=UTC)


def _intent(tenant_id, order, provider="bancard_vpos", created=OLD, status="paid"):
    return PaymentIntentModel(
        tenant_id=tenant_id, provider=provider, order_id=order, amount=100.0, currency="PYG",
        status=status, checkout_url="https://pay.example/SECRET-TOKEN", provider_reference=f"r{order}",
        created_at=created,
    )


def _event(tenant_id, source, external_id, created=OLD):
    return ProcessedEventModel(
        tenant_id=tenant_id, source=source, external_id=external_id, created_at=created
    )


@pytest.fixture
async def world(migrated_db):
    factory, make_tenant = migrated_db
    acme = await make_tenant(slug="acme", payment_provider=PaymentProvider.BANCARD_VPOS)
    other = await make_tenant(slug="other", shopify_shop_domain="other.myshopify.com")
    async with factory() as session:
        session.add_all(
            [
                _intent(acme.id, "1"),
                _intent(acme.id, "2", status="pending"),
                _intent(acme.id, "3", created=NEW),
                _intent(acme.id, "4", provider="pagopar"),
                _intent(other.id, "1"),
                _event(acme.id, "payment:bancard_vpos", "e1"),
                _event(acme.id, "payment_link:bancard_vpos", "e2"),
                _event(acme.id, "payment:bancard_vpos", "e3", created=NEW),
                _event(acme.id, "payment:pagopar", "e4"),
                _event(acme.id, "shopify_order", "e5"),
                _event(other.id, "payment:bancard_vpos", "e1"),
            ]
        )
        await session.commit()
    return factory, acme, other


async def _count(factory, model, tenant_id=None):
    async with factory() as session:
        stmt = select(func.count()).select_from(model)
        if tenant_id is not None:
            stmt = stmt.where(model.tenant_id == tenant_id)
        return (await session.execute(stmt)).scalar_one()


async def _audits(factory):
    async with factory() as session:
        return list(await session.scalars(select(AuditLogModel).where(
            AuditLogModel.action == "payments.purged")))


async def test_dry_run_is_the_default_and_deletes_nothing(world):
    factory, *_ = world
    lines: list[str] = []

    result = await run_purge(factory, tenant_slug="acme", out=lines.append)

    assert result.dry_run is True and (result.intents, result.events) == (3, 3)
    assert await _count(factory, PaymentIntentModel) == 5
    assert await _count(factory, ProcessedEventModel) == 6
    assert await _audits(factory) == []
    text = "\n".join(lines)
    assert "DRY RUN" in text and "--confirm-tenant acme" in text
    assert "SECRET-TOKEN" not in text


async def test_confirmed_purge_removes_only_that_tenants_provider_payment_rows(world):
    factory, acme, other = world

    result = await run_purge(factory, tenant_slug="acme", confirm_tenant="acme", out=lambda _: None)

    assert result.dry_run is False and (result.intents, result.events) == (3, 3)
    async with factory() as session:
        intents = {(i.tenant_id, i.provider, i.order_id) async for i in await session.stream_scalars(select(PaymentIntentModel))}
        events = {(e.tenant_id, e.source, e.external_id) async for e in await session.stream_scalars(select(ProcessedEventModel))}
    assert intents == {(acme.id, "pagopar", "4"), (other.id, "bancard_vpos", "1")}
    assert events == {
        (acme.id, "payment:pagopar", "e4"),
        (acme.id, "shopify_order", "e5"),
        (other.id, "payment:bancard_vpos", "e1"),
    }
    (entry,) = await _audits(factory)
    assert entry.tenant_id == acme.id and entry.actor == "cli"
    assert entry.payload["intents_deleted"] == 3 and entry.payload["events_deleted"] == 3
    assert entry.payload["provider"] == "bancard_vpos"


async def test_a_second_run_deletes_nothing_more(world):
    factory, *_ = world
    await run_purge(factory, tenant_slug="acme", confirm_tenant="acme", out=lambda _: None)

    again = await run_purge(factory, tenant_slug="acme", confirm_tenant="acme", out=lambda _: None)

    assert (again.intents, again.events) == (0, 0)


@pytest.mark.parametrize("confirm", ["", "other", "ACME", " acme"])
async def test_confirmation_must_repeat_the_tenant_slug_exactly(world, confirm):
    factory, *_ = world

    with pytest.raises(PurgeRefusedError):
        await run_purge(factory, tenant_slug="acme", confirm_tenant=confirm, out=lambda _: None)

    assert await _count(factory, PaymentIntentModel) == 5
    assert await _audits(factory) == []


async def test_date_bounds_protect_newer_history(world):
    factory, acme, _ = world

    result = await run_purge(
        factory, tenant_slug="acme", created_before=datetime(2026, 10, 15, tzinfo=UTC),
        confirm_tenant="acme", out=lambda _: None,
    )

    assert (result.intents, result.events) == (2, 2)
    async with factory() as session:
        left = await session.scalars(select(PaymentIntentModel.order_id).where(
            PaymentIntentModel.tenant_id == acme.id, PaymentIntentModel.provider == "bancard_vpos"))
        assert list(left) == ["3"]


async def test_created_after_bound_is_inclusive(world):
    factory, *_ = world

    result = await run_purge(factory, tenant_slug="acme", created_after=NEW, out=lambda _: None)

    assert (result.intents, result.events) == (1, 1)


async def test_unknown_tenant_is_refused(world):
    factory, *_ = world
    with pytest.raises(TenantNotFoundError):
        await run_purge(factory, tenant_slug="ghost", out=lambda _: None)


async def test_a_production_configured_tenant_needs_an_extra_flag(world):
    factory, acme, _ = world
    from bims_shopify.adapters.persistence.models import TenantModel

    async with factory() as session:
        tenant = await session.get(TenantModel, acme.id)
        tenant.provider_config = {"environment": "production"}
        await session.commit()

    lines: list[str] = []
    dry = await run_purge(factory, tenant_slug="acme", out=lines.append)
    assert dry.dry_run and "production" in "\n".join(lines).lower()
    with pytest.raises(PurgeRefusedError):
        await run_purge(factory, tenant_slug="acme", confirm_tenant="acme", out=lambda _: None)
    assert await _count(factory, PaymentIntentModel) == 5

    done = await run_purge(
        factory, tenant_slug="acme", confirm_tenant="acme", allow_production=True, out=lambda _: None
    )
    assert done.intents == 3


def test_the_purge_is_not_reachable_over_http():
    from bims_shopify import main

    assert not [p for p in main.create_app().openapi()["paths"] if "purge" in p.lower()]


def test_cli_defaults_are_a_dry_run_and_dates_are_utc_midnight():
    from bims_shopify.ops.purge_payments import _build_arg_parser

    args = _build_arg_parser().parse_args(["acme", "--created-before", "2026-10-15"])

    assert args.confirm_tenant is None and args.allow_production is False
    assert args.provider == "bancard_vpos"
    assert args.created_before == datetime(2026, 10, 15, tzinfo=UTC)
