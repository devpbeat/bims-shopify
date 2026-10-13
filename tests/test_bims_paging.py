"""Tests for the shared BIMS products paging helper."""
from __future__ import annotations

from typing import Any

from bims_shopify.adapters.bims import paging
from bims_shopify.adapters.bims.paging import paginate_products


def _rows(start: int, n: int) -> list[dict[str, Any]]:
    return [{"Product": {"id": i, "code2": f"S{i}"}} for i in range(start, start + n)]


def _scripted(pages: list[dict[str, Any]]):
    """A fetch_page stub serving ``pages`` in order and recording the offsets asked."""
    offsets: list[int] = []

    async def fetch_page(offset: int) -> dict[str, Any]:
        offsets.append(offset)
        index = len(offsets) - 1
        return pages[index] if index < len(pages) else {"data": []}

    return fetch_page, offsets


async def test_short_page_mid_catalog_does_not_terminate_the_pull():
    fetch, _offsets = _scripted(
        [
            {"count": 7, "data": _rows(1, 3)},  # short: the page size is 5
            {"count": 7, "data": _rows(4, 4)},
            {"count": 7, "data": []},
        ]
    )
    pull = await paginate_products(fetch)
    assert [r["id"] for r in pull.rows] == [1, 2, 3, 4, 5, 6, 7]
    assert pull.stop_reason == "empty_page"


async def test_empty_page_terminates_the_pull():
    fetch, offsets = _scripted([{"data": _rows(1, 2)}, {"data": []}])
    pull = await paginate_products(fetch)
    assert len(pull.rows) == 2
    assert offsets == [0, 2]
    assert pull.stop_reason == "empty_page"


async def test_offset_advances_by_rows_received_not_a_fixed_page_size():
    fetch, offsets = _scripted(
        [{"data": _rows(1, 3)}, {"data": _rows(4, 2)}, {"data": _rows(6, 5)}, {"data": []}]
    )
    await paginate_products(fetch)
    assert offsets == [0, 3, 5, 10]


async def test_unwrapped_rows_are_accepted():
    fetch, _ = _scripted([{"data": [{"id": 1}, {"id": 2}]}, {"data": []}])
    pull = await paginate_products(fetch)
    assert [r["id"] for r in pull.rows] == [1, 2]


async def test_complete_false_when_rows_fall_short_of_reported_count():
    fetch, _ = _scripted([{"count": 10, "data": _rows(1, 4)}, {"count": 10, "data": []}])
    pull = await paginate_products(fetch)
    assert pull.reported_count == 10
    assert pull.complete is False
    assert pull.summary() == {
        "rows": 4,
        "reported_count": 10,
        "complete": False,
        "pages": 2,
        "stop_reason": "empty_page",
        "warnings": pull.warnings,
        "duration_seconds": pull.duration_seconds,
    }
    assert any("4" in w and "10" in w for w in pull.warnings)


async def test_complete_true_when_rows_reach_reported_count():
    fetch, _ = _scripted([{"count": 4, "data": _rows(1, 4)}, {"count": 4, "data": []}])
    pull = await paginate_products(fetch)
    assert pull.complete is True
    assert pull.warnings == []


async def test_complete_is_unknown_not_false_when_server_sends_no_count():
    fetch, _ = _scripted([{"data": _rows(1, 4)}, {"data": []}])
    pull = await paginate_products(fetch)
    assert pull.reported_count is None
    assert pull.complete is None


async def test_missing_none_and_garbage_counts_are_unknown():
    for bad in (None, "", "n/a", True):
        fetch, _ = _scripted([{"count": bad, "data": _rows(1, 2)}, {"data": []}])
        pull = await paginate_products(fetch)
        assert pull.reported_count is None
        assert pull.complete is None


async def test_count_sent_as_a_string_is_parsed():
    fetch, _ = _scripted([{"count": "4", "data": _rows(1, 4)}, {"data": []}])
    pull = await paginate_products(fetch)
    assert pull.reported_count == 4
    assert pull.complete is True


async def test_zero_count_is_a_real_count_not_unknown():
    fetch, _ = _scripted([{"count": 0, "data": []}])
    pull = await paginate_products(fetch)
    assert pull.reported_count == 0
    assert pull.complete is True


async def test_inconsistent_counts_across_pages_are_flagged():
    fetch, _ = _scripted(
        [{"count": 5, "data": _rows(1, 2)}, {"count": 2, "data": _rows(3, 2)}, {"data": []}]
    )
    pull = await paginate_products(fetch)
    assert pull.reported_count == 5  # the first response wins
    assert any("inconsistent" in w for w in pull.warnings)


async def test_repeated_page_guard_trips_instead_of_looping_forever():
    same = {"count": 99, "data": _rows(1, 3)}
    fetch, offsets = _scripted([same] * 50)
    pull = await paginate_products(fetch)
    assert pull.stop_reason == "repeated_page"
    assert len(pull.rows) == 3  # no duplicates accumulated
    assert len(offsets) == 2
    assert pull.complete is False
    assert any("repeated" in w for w in pull.warnings)


async def test_max_pages_guard_trips_on_an_endless_catalog():
    counter = {"next": 1}

    async def endless(offset: int) -> dict[str, Any]:
        start = counter["next"]
        counter["next"] += 2
        return {"data": _rows(start, 2)}

    pull = await paginate_products(endless, max_pages=5)
    assert pull.stop_reason == "max_pages"
    assert pull.pages == 5
    assert len(pull.rows) == 10
    assert pull.complete is False
    assert any("max" in w.lower() for w in pull.warnings)


async def test_truncation_is_logged_as_a_warning(monkeypatch):
    events: list[tuple[str, dict]] = []

    class _Recorder:
        def warning(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

    monkeypatch.setattr(paging, "logger", _Recorder())
    fetch, _ = _scripted([{"count": 10, "data": _rows(1, 4)}, {"data": []}])
    await paginate_products(fetch)
    assert events
    event, fields = events[0]
    assert event == "bims_catalog_pull_incomplete"
    assert fields["rows"] == 4 and fields["reported_count"] == 10


async def test_complete_pull_logs_nothing(monkeypatch):
    events: list[str] = []

    class _Recorder:
        def warning(self, event: str, **kwargs: Any) -> None:
            events.append(event)

    monkeypatch.setattr(paging, "logger", _Recorder())
    fetch, _ = _scripted([{"count": 2, "data": _rows(1, 2)}, {"data": []}])
    await paginate_products(fetch)
    assert events == []


async def test_repeated_page_after_reaching_the_count_is_still_complete():
    """Some servers re-serve the last page past the end; the count proves nothing was lost."""
    same = {"count": 3, "data": _rows(1, 3)}
    fetch, _offsets = _scripted([same] * 5)
    pull = await paginate_products(fetch)
    assert pull.stop_reason == "repeated_page"
    assert pull.complete is True


async def test_abnormal_stop_without_a_count_is_incomplete():
    same = {"data": _rows(1, 3)}
    fetch, _offsets = _scripted([same] * 5)
    pull = await paginate_products(fetch)
    assert pull.complete is False


async def test_rows_without_an_id_still_trip_the_repeated_page_guard():
    same = {"data": [{"code2": "A"}, {"code2": "B"}]}
    fetch, offsets = _scripted([same] * 50)
    pull = await paginate_products(fetch)
    assert pull.stop_reason == "repeated_page"
    assert len(pull.rows) == 2
    assert len(offsets) == 2


# -- windowed (concurrent) paging ---------------------------------------------


def _catalog_server(total: int, page_size: int, *, short_page_at: int | None = None):
    """A fake BIMS serving ``total`` rows; records offsets and max in-flight requests."""
    import asyncio

    state = {"in_flight": 0, "max_in_flight": 0, "offsets": []}

    async def fetch_page(offset: int) -> dict[str, Any]:
        state["offsets"].append(offset)
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        await asyncio.sleep(0.01)
        state["in_flight"] -= 1
        size = page_size
        if short_page_at is not None and offset == short_page_at:
            size = page_size - 2
        n = max(0, min(size, total - offset))
        return {"count": total, "data": _rows(offset + 1, n)}

    return fetch_page, state


async def test_windowed_pull_equals_sequential_pull():
    fetch_a, _ = _catalog_server(23, 5)
    fetch_b, _ = _catalog_server(23, 5)
    sequential = await paginate_products(fetch_a)
    windowed = await paginate_products(fetch_b, page_size=5, concurrency=4)
    assert windowed.rows == sequential.rows
    assert windowed.stop_reason == "empty_page"
    assert windowed.complete is True
    assert windowed.reported_count == 23
    assert windowed.pages == sequential.pages


async def test_windowed_overshoot_is_a_normal_termination():
    # 10 rows / 5 per page: page 3 is empty; the window overshoots further.
    fetch, _ = _catalog_server(10, 5)
    pull = await paginate_products(fetch, page_size=5, concurrency=4)
    assert len(pull.rows) == 10
    assert pull.stop_reason == "empty_page"
    assert pull.complete is True
    assert pull.warnings == []


async def test_windowed_never_exceeds_concurrency():
    fetch, state = _catalog_server(100, 5)
    await paginate_products(fetch, page_size=5, concurrency=3)
    assert 1 < state["max_in_flight"] <= 3


async def test_windowed_short_page_mid_catalog_loses_no_rows():
    # The page at offset 5 comes back short (3 of 5): speculative offsets after
    # it are wrong and must be re-requested from the true offset.
    fetch_a, _ = _catalog_server(23, 5, short_page_at=5)
    fetch_b, state = _catalog_server(23, 5, short_page_at=5)
    sequential = await paginate_products(fetch_a)
    windowed = await paginate_products(fetch_b, page_size=5, concurrency=4)
    assert [r["id"] for r in windowed.rows] == [r["id"] for r in sequential.rows]
    assert len({r["id"] for r in windowed.rows}) == len(windowed.rows)
    assert 8 in state["offsets"]  # re-requested at the real offset 5 + 3


async def test_windowed_deduplicates_rows_across_pages():
    pages = {
        0: {"data": _rows(1, 3)},
        3: {"data": _rows(2, 3)},  # overlaps ids 2, 3 with the previous page
        6: {"data": []},
    }

    async def fetch(offset: int) -> dict[str, Any]:
        return pages.get(offset, {"data": []})

    pull = await paginate_products(fetch, page_size=3, concurrency=3)
    assert [r["id"] for r in pull.rows] == [1, 2, 3, 4]


async def test_windowed_repeated_page_stops_incomplete():
    async def fetch(offset: int) -> dict[str, Any]:
        return {"data": _rows(1, 3)}  # server ignores offset

    pull = await paginate_products(fetch, page_size=3, concurrency=4)
    assert pull.stop_reason == "repeated_page"
    assert pull.complete is False
    assert len(pull.rows) == 3


async def test_windowed_max_pages_never_requests_beyond_the_cap():
    fetch, state = _catalog_server(1000, 5)
    pull = await paginate_products(fetch, page_size=5, concurrency=4, max_pages=6)
    assert pull.stop_reason == "max_pages"
    assert pull.pages == 6
    assert len(state["offsets"]) == 6
    assert pull.complete is False


async def test_windowed_failing_page_propagates():
    import pytest

    async def fetch(offset: int) -> dict[str, Any]:
        if offset == 10:
            raise RuntimeError("boom")
        return {"data": _rows(offset + 1, 5)}

    with pytest.raises(RuntimeError):
        await paginate_products(fetch, page_size=5, concurrency=4)


async def test_pull_records_duration():
    fetch, _ = _catalog_server(10, 5)
    pull = await paginate_products(fetch, page_size=5, concurrency=2)
    assert pull.duration_seconds >= 0.01
    assert pull.summary()["duration_seconds"] == pull.duration_seconds
