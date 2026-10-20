"""Use case: the merchant payments report (rows plus chart-ready aggregates)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from bims_shopify.domain.payment_report import MAX_RANGE_DAYS, ReportFilters
from bims_shopify.ports.repositories import PaymentReportRepository

DEFAULT_WINDOW_DAYS = 30


class InvalidReportFilters(ValueError):
    """The requested filters are inconsistent or too wide."""


def resolve_filters(
    date_from: date | None,
    date_to: date | None,
    status: str | None,
    provider: str | None,
    today: date,
) -> ReportFilters:
    """Fill the default window and validate the range.

    An omitted ``date_to`` is today and an omitted ``date_from`` is
    ``DEFAULT_WINDOW_DAYS`` before ``date_to``, so the per-day series is
    always bounded.
    """
    end = date_to or today
    start = date_from or end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if start > end:
        raise InvalidReportFilters("date_from must not be after date_to")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise InvalidReportFilters(f"The date range must not exceed {MAX_RANGE_DAYS} days")
    return ReportFilters(
        date_from=start,
        date_to=end,
        status=(status or "").strip() or None,
        provider=(provider or "").strip() or None,
    )


def filters_to_dict(filters: ReportFilters) -> dict[str, Any]:
    return {
        "date_from": filters.date_from.isoformat() if filters.date_from else None,
        "date_to": filters.date_to.isoformat() if filters.date_to else None,
        "status": filters.status,
        "provider": filters.provider,
    }


async def build_report(
    repo: PaymentReportRepository,
    tenant_id: int,
    filters: ReportFilters,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    rows = await repo.report_rows(tenant_id, filters, limit, offset)
    aggregates = await repo.report_aggregates(tenant_id, filters)
    return {
        "filters": filters_to_dict(filters),
        "rows": rows,
        "total": sum(item["count"] for item in aggregates["by_status"]),
        "limit": limit,
        "offset": offset,
        "by_status": aggregates["by_status"],
        "by_day": aggregates["by_day"],
    }
