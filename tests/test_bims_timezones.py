"""Unit tests for the BIMS naive-local-time boundary helpers."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from bims_shopify.adapters.bims.timezones import from_bims_local, to_bims_local

ASU = "America/Asuncion"


def test_to_bims_local_converts_aware_utc_to_naive_local_string():
    dt_utc = datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC)
    assert to_bims_local(dt_utc, ASU) == "2026-01-01 09:30:45"


def test_to_bims_local_accepts_any_aware_tzinfo():
    dt_local = datetime(2026, 1, 1, 9, 30, 45, tzinfo=ZoneInfo(ASU))
    assert to_bims_local(dt_local, "UTC") == "2026-01-01 12:30:45"


def test_to_bims_local_rejects_naive_datetime():
    naive = datetime.fromisoformat("2026-01-01T12:30:45")
    with pytest.raises(ValueError, match="aware"):
        to_bims_local(naive, ASU)


def test_to_bims_local_rejects_unknown_timezone():
    dt_utc = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        to_bims_local(dt_utc, "Not/AZone")


def test_from_bims_local_parses_naive_local_string_to_aware_utc():
    result = from_bims_local("2026-01-01 09:30:45", ASU)
    assert result == datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC)
    assert result.tzinfo is not None


def test_from_bims_local_rejects_unknown_timezone():
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        from_bims_local("2026-01-01 09:30:45", "Not/AZone")


def test_round_trip_aware_utc_through_bims_local_and_back():
    original = datetime(2026, 6, 15, 23, 25, 54, tzinfo=UTC)
    local_string = to_bims_local(original, ASU)
    recovered = from_bims_local(local_string, ASU)
    # Only second precision survives the BIMS wire format.
    assert recovered == original.replace(microsecond=0)


def test_round_trip_arbitrary_aware_timezone_through_bims_local_and_back():
    original = datetime(2026, 6, 15, 20, 25, 54, tzinfo=ZoneInfo("America/New_York"))
    local_string = to_bims_local(original, ASU)
    recovered = from_bims_local(local_string, ASU)
    assert recovered == original.astimezone(UTC).replace(microsecond=0)
