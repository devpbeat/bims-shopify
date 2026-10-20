"""Filters and limits for the merchant payments report."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MAX_RANGE_DAYS = 366
"""Widest date range one report may span; bounds the per-day chart series."""

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
EXPORT_ROW_CAP = 10_000
"""Hard cap on exported rows so a huge range cannot exhaust memory."""


@dataclass(frozen=True)
class ReportFilters:
    """Inclusive UTC date range plus optional exact status/provider matches."""

    date_from: date | None = None
    date_to: date | None = None
    status: str | None = None
    provider: str | None = None
