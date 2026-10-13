"""Shared pager for BIMS ``/api/products/index.json``.

Every product pull used to stop at the FIRST page shorter than the requested
limit and ignore the envelope's own ``count``. A short page mid-catalog
therefore truncated the pull silently while reporting success. This helper
stops only on an EMPTY page, advances ``offset`` by the rows actually
received, guards against a non-terminating loop, and reports whether the
accumulated rows reach the server-reported ``count``.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bims_shopify.logging import get_logger

logger = get_logger(__name__)

#: Upper bound on pages per pull (250 rows/page => 500k rows); a safety net
#: against a server that keeps answering, never expected to be reached.
DEFAULT_MAX_PAGES = 2000

#: Pages requested concurrently per window. BIMS is the client's live billing
#: ERP (slow, flaky), so this is deliberately conservative; override per call.
DEFAULT_PAGE_CONCURRENCY = 4

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
    #: Wall-clock seconds the whole pull took (requests included).
    duration_seconds: float = 0.0

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
            "duration_seconds": self.duration_seconds,
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
    page_size: int | None = None,
    concurrency: int | None = None,
) -> PagedPull:
    """Page through ``fetch_page(offset)`` until an empty page.

    ``fetch_page`` returns the raw response envelope for the given offset.
    Rows are unwrapped from ``{"Product": {...}}`` and de-duplicated by
    ``id`` (by content when a row has none). The pull also stops, flagged as incomplete, when a page brings no
    new row (the server is ignoring ``offset``) or after ``max_pages``.

    With ``page_size`` given and ``concurrency > 1`` the pull is WINDOWED: the
    next ``concurrency`` offsets are requested together (never more in flight
    than that), then the responses are consumed strictly IN OFFSET ORDER by
    exactly the same logic as the sequential loop. The offsets of a window are
    a guess (``offset + k * page_size``); the true next offset is the rows
    actually received. If a page comes back short the guess for every later
    page in the window is wrong, so those responses are discarded unread and
    the window restarts from the true offset. Pages past the end come back
    empty and are discarded after the first empty one: normal termination.
    Without ``page_size`` the pull is sequential. ``concurrency=None`` means
    ``DEFAULT_PAGE_CONCURRENCY``.
    """
    started = time.perf_counter()
    pull = PagedPull()
    seen_ids: set[str] = set()
    offset = 0
    counts_disagree = False
    if concurrency is None:
        concurrency = DEFAULT_PAGE_CONCURRENCY
    window = concurrency if page_size and concurrency > 1 else 1
    done = False

    while not done and pull.pages < max_pages:
        # Never request beyond the page cap, even speculatively.
        size = min(window, max_pages - pull.pages)
        offsets = [offset + k * (page_size or 0) for k in range(size)] if size > 1 else [offset]
        # return_exceptions: a speculative page past the end of the catalog
        # must not fail a pull that terminates before reaching it. An error is
        # raised only when its page is actually consumed, in order.
        results = await asyncio.gather(*(fetch_page(o) for o in offsets), return_exceptions=True)

        for requested, body in zip(offsets, results, strict=True):
            if requested != offset:
                # An earlier page was short: this speculative offset is wrong.
                break
            if isinstance(body, BaseException):
                raise body
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
                done = True
                break

            new_rows = 0
            for item in page:
                row = item.get("Product") or item
                row_id = row.get("id")
                # Rows without an id fall back to their content so a repeated page
                # of id-less rows is still detected.
                key = (
                    str(row_id)
                    if row_id is not None
                    else json.dumps(row, sort_keys=True, default=str)
                )
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
                done = True
                break
            offset += len(page)
    else:
        if not done:
            pull.stop_reason = STOP_MAX_PAGES
            pull.warnings.append(f"stopped after the max of {max_pages} pages")

    if pull.reported_count is not None and len(pull.rows) < pull.reported_count:
        pull.warnings.append(
            f"truncated pull: {len(pull.rows)} rows received but the server reports "
            f"{pull.reported_count}"
        )
    pull.duration_seconds = time.perf_counter() - started
    if pull.complete is False:
        logger.warning(
            "bims_catalog_pull_incomplete",
            rows=len(pull.rows),
            reported_count=pull.reported_count,
            stop_reason=pull.stop_reason,
            warnings=pull.warnings,
        )
    return pull
