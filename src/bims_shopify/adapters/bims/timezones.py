"""Boundary conversions between aware UTC datetimes and BIMS's naive local time.

BIMS `last_update` fields (in response bodies and as the sync filter query
param) are naive LOCAL time in the tenant's IANA zone (America/Asuncion by
default), formatted as ``'YYYY-MM-DD HH:MM:SS'`` — confirmed empirically: a
BIMS response body ``last_update`` of ``20:25:54`` corresponded to an HTTP
``Date`` header of ``23:25:54 GMT`` (body time = UTC - 3h = America/Asuncion
local time). Every other layer of this application works in aware UTC
datetimes; these two functions are the ONLY place naive datetimes should be
constructed or consumed for BIMS.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BIMS_LOCAL_FORMAT = "%Y-%m-%d %H:%M:%S"


def to_bims_local(dt_aware: datetime, tz: str) -> str:
    """Convert an aware datetime to BIMS's naive local string format.

    `dt_aware` may carry any tzinfo (UTC or otherwise); it is converted to
    `tz` before formatting. Raises `ValueError` if `dt_aware` is naive (the
    caller must supply an aware datetime — naive datetimes are ambiguous and
    must not cross this boundary) or if `tz` is not a valid IANA zone key.
    """
    if dt_aware.tzinfo is None:
        raise ValueError("to_bims_local requires an aware datetime, got a naive one")
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {tz!r}") from exc
    local = dt_aware.astimezone(zone)
    # Intentional tzinfo-stripping: this is the one place a naive local
    # string is meant to be produced, for the BIMS wire format.
    naive_local = local.replace(tzinfo=None)
    return naive_local.strftime(BIMS_LOCAL_FORMAT)


def from_bims_local(s: str, tz: str) -> datetime:
    """Parse a naive BIMS local-time string into an aware UTC datetime."""
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {tz!r}") from exc
    naive = datetime.strptime(s, BIMS_LOCAL_FORMAT).replace(tzinfo=zone)
    return naive.astimezone(ZoneInfo("UTC"))
