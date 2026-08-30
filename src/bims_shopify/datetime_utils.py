"""Shared datetime normalization helpers.

All datetimes must be timezone-aware UTC internally. The one place naive
datetimes are still allowed to exist is at the BIMS adapter boundary (see
``adapters.bims.timezones``), and, transiently, when reading rows written by
SQLite: SQLAlchemy's ``DateTime(timezone=True)`` silently stores and returns
naive datetimes on SQLite (it has no real tz-aware column type), so values
read back from a sqlite-backed test/dev database need to be re-tagged as UTC
before they re-enter the application.
"""
from __future__ import annotations

from datetime import UTC, datetime


def ensure_aware_utc(dt: datetime | None) -> datetime | None:
    """Return `dt` as an aware UTC datetime.

    - ``None`` is passed through unchanged.
    - An already-aware datetime is returned unchanged (its offset is trusted,
      not forced to UTC), since this is not a conversion helper.
    - A naive datetime is assumed to already represent UTC clock time (which
      is what every naive datetime read back from SQLite via this codebase's
      repositories actually is) and is tagged with `UTC` tzinfo accordingly.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
