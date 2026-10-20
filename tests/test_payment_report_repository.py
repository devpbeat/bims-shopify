"""Tenant-scoped, filtered reporting queries over ``payment_intents``."""
from __future__ import annotations

from datetime import UTC, date, datetime

from bims_shopify.adapters.persistence.models import PaymentIntentModel
from bims_shopify.adapters.persistence.payment_intent_repository import (
    SqlAlchemyPaymentIntentRepository,
)
from bims_shopify.domain.payment_report import ReportFilters


def _row(tenant_id, order_id, *, amount, status, day, provider="bancard_vpos", hour=12):
    return PaymentIntentModel(
        tenant_id=tenant_id,
        provider=provider,
        order_id=order_id,
        amount=amount,
        currency="PYG",
        status=status,
        checkout_url="https://pay.example.com/?token=SECRET",
        provider_reference=f"ref-{order_id}",
        process_id=f"pid-{order_id}",
        created_at=datetime(2026, 10, day, hour, 0, tzinfo=UTC),
    )


async def _seed(factory, make_tenant):
    acme = await make_tenant()
    other = await make_tenant(slug="other", shopify_shop_domain="other.myshopify.com")
    async with factory() as session:
        session.add_all(
            [
                _row(acme.id, "A1", amount=100000, status="confirmed", day=1),
                _row(acme.id, "A2", amount=50000, status="pending", day=1),
                _row(acme.id, "A3", amount=25000, status="confirmed", day=2),
                _row(acme.id, "A4", amount=10000, status="failed", day=3, provider="pagopar"),
                _row(other.id, "O1", amount=999999, status="confirmed", day=1),
            ]
        )
        await session.commit()
    return acme, other


async def test_rows_are_scoped_to_the_tenant_and_omit_the_checkout_url(migrated_db):
    factory, make_tenant = migrated_db
    acme, _other = await _seed(factory, make_tenant)
    async with factory() as session:
        repo = SqlAlchemyPaymentIntentRepository(session)
        rows = await repo.report_rows(acme.id, ReportFilters(), limit=100, offset=0)
    assert {r["order_id"] for r in rows} == {"A1", "A2", "A3", "A4"}
    assert all("checkout_url" not in r for r in rows)
    assert all(isinstance(r["id"], int) and r["process_id"] for r in rows)


async def test_filters_by_status_provider_and_inclusive_date_range(migrated_db):
    factory, make_tenant = migrated_db
    acme, _ = await _seed(factory, make_tenant)
    async with factory() as session:
        repo = SqlAlchemyPaymentIntentRepository(session)
        by_status = await repo.report_rows(
            acme.id, ReportFilters(status="confirmed"), limit=100, offset=0
        )
        by_provider = await repo.report_rows(
            acme.id, ReportFilters(provider="pagopar"), limit=100, offset=0
        )
        by_range = await repo.report_rows(
            acme.id,
            ReportFilters(date_from=date(2026, 10, 2), date_to=date(2026, 10, 2)),
            limit=100,
            offset=0,
        )
    assert {r["order_id"] for r in by_status} == {"A1", "A3"}
    assert {r["order_id"] for r in by_provider} == {"A4"}
    assert {r["order_id"] for r in by_range} == {"A3"}


async def test_limit_and_offset_page_newest_first(migrated_db):
    factory, make_tenant = migrated_db
    acme, _ = await _seed(factory, make_tenant)
    async with factory() as session:
        repo = SqlAlchemyPaymentIntentRepository(session)
        first = await repo.report_rows(acme.id, ReportFilters(), limit=2, offset=0)
        second = await repo.report_rows(acme.id, ReportFilters(), limit=2, offset=2)
    assert [r["order_id"] for r in first] == ["A4", "A3"]
    assert len(second) == 2


async def test_aggregates_per_status_and_per_day_ignore_paging_and_other_tenants(migrated_db):
    factory, make_tenant = migrated_db
    acme, _ = await _seed(factory, make_tenant)
    async with factory() as session:
        agg = await SqlAlchemyPaymentIntentRepository(session).report_aggregates(
            acme.id, ReportFilters()
        )
    by_status = {s["status"]: s for s in agg["by_status"]}
    assert by_status["confirmed"] == {"status": "confirmed", "count": 2, "total_amount": 125000}
    assert by_status["pending"]["count"] == 1
    assert by_status["failed"]["total_amount"] == 10000
    assert agg["by_day"] == [
        {"date": "2026-10-01", "count": 2, "total_amount": 150000},
        {"date": "2026-10-02", "count": 1, "total_amount": 25000},
        {"date": "2026-10-03", "count": 1, "total_amount": 10000},
    ]


async def test_another_tenant_sees_only_its_own_aggregates(migrated_db):
    factory, make_tenant = migrated_db
    _acme, other = await _seed(factory, make_tenant)
    async with factory() as session:
        agg = await SqlAlchemyPaymentIntentRepository(session).report_aggregates(
            other.id, ReportFilters()
        )
    assert agg["by_status"] == [{"status": "confirmed", "count": 1, "total_amount": 999999}]
