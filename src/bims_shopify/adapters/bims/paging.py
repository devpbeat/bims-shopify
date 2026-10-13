"""Shared pager for BIMS ``/api/products/index.json``.

Every product pull used to stop at the FIRST page shorter than the requested
limit and ignore the envelope's own ``count``. A short page mid-catalog
therefore truncated the pull silently while reporting success. This helper
stops only on an EMPTY page, advances ``offset`` by the rows actually
received, guards against a non-terminating loop, and reports whether the
accumulated rows reach the server-reported ``count``.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bims_shopify.logging import get_logger

logger = get_logger(__name__)

#: Upper bound on pages per pull (250 rows/page => 500k rows); a safety net
#: against a server that keeps answering, never expected to be reached.
DEFAULT_MAX_PAGES = 2000

STOP_EMPTY_PAGE = "empty_page"
STOP_REPEATED_PAGE = "repeated_page"
STOP_MAX_PAGES = "max_pages"


@dataclass
class PagedPull:
    """Rows of a paged pull plus what is known about its completeness.

    ``reported_count`` is the envelope ``count`` of the first response that
    carried a usable one, or ``None`` when the server sent none -- unknown,
    not zero. With a count, ``complete`` is whether the rows reach it. Without
    one it is ``None`` (cannot be judged) after a normal end, ``False`` after
    an abnormal stop (repeated page / page cap).
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    reported_count: int | None = None
    pages: int = 0
    stop_reason: str = STOP_EMPTY_PAGE
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool | None:
        if self.reported_count is not None:
            return len(self.rows) >= self.reported_count
        return None if self.stop_reason == STOP_EMPTY_PAGE else False

    def summary(self) -> dict[str, Any]:
        return {
            "rows": len(self.rows),
            "reported_count": self.reported_count,
            "complete": self.complete,
            "pages": self.pages,
            "stop_reason": self.stop_reason,
            "warnings": self.warnings,
        }


def _parse_count(value: Any) -> int | None:
    """BIMS sends ``count`` as an int or a numeric string; anything else is unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


async def paginate_products(
    fetch_page: Callable[[int], Awaitable[dict[str, Any]]],
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> PagedPull:
    """Page through ``fetch_page(offset)`` until an empty page.

    ``fetch_page`` returns the raw response envelope for the given offset.
    Rows are unwrapped from ``{"Product": {...}}`` and de-duplicated by
    ``id``. The pull also stops, flagged as incomplete, when a page brings no
    new row (the server is ignoring ``offset``) or after ``max_pages``.
    """
    pull = PagedPull()
    seen_ids: set[str] = set()
    offset = 0
    counts_disagree = False

    while pull.pages < max_pages:
        body = await fetch_page(offset)
        pull.pages += 1

        count = _parse_count(body.get("count"))
        if count is not None:
            if pull.reported_count is None:
                pull.reported_count = count
            elif count != pull.reported_count and not counts_disagree:
                counts_disagree = True
                pull.warnings.append(
                    f"inconsistent count across pages ({pull.reported_count} then {count}); "
                    "it may not be a catalog total"
                )

        page = body.get("data") or []
        if not page:
            pull.stop_reason = STOP_EMPTY_PAGE
            break

        new_rows = 0
        for item in page:
            row = item.get("Product") or item
            row_id = row.get("id")
            if row_id is not None:
                key = str(row_id)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
            pull.rows.append(row)
            new_rows += 1

        if new_rows == 0:
            pull.stop_reason = STOP_REPEATED_PAGE
            pull.warnings.append(
                f"repeated page at offset {offset}: no new rows, the server may be "
                "ignoring offset; stopping"
            )
            break
        offset += len(page)
    else:
        pull.stop_reason = STOP_MAX_PAGES
        pull.warnings.append(f"stopped after the max of {max_pages} pages")

    if pull.reported_count is not None and len(pull.rows) < pull.reported_count:
        pull.warnings.append(
            f"truncated pull: {len(pull.rows)} rows received but the server reports "
            f"{pull.reported_count}"
        )
    if pull.complete is False:
        logger.warning(
            "bims_catalog_pull_incomplete",
            rows=len(pull.rows),
            reported_count=pull.reported_count,
            stop_reason=pull.stop_reason,
            warnings=pull.warnings,
        )
    return pull
