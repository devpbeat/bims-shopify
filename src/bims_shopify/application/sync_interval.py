"""Minimum safe worker intervals, derived from the system's own measured runs.

A scheduled worker is only safe if one run reliably finishes well before the
next tick. Rather than trusting an operator's guess, the floor is computed
from the durations of recent completed runs.

Two workers, two histories: every PULL is a full catalog pull (history key
``"full"``) and every PUSH run is recorded under ``"push"``. They must never
share a history: a push takes seconds, and measuring the pull interval by it
would unlock a schedule BIMS cannot sustain.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Protocol

#: Floor = slowest recent run * SAFETY_FACTOR, rounded up to whole minutes.
#: 3x means a sync occupies at most about a third of its interval, which
#: absorbs a BIMS/Shopify slowdown of up to 3x (busy trading days, a larger
#: catalog pull) before runs start overlapping and being skipped.
SAFETY_FACTOR = 3

#: Never allow intervals shorter than this, however fast the runs look.
ABSOLUTE_MIN_INTERVAL_MINUTES = 5

#: Floor used while a tenant has no measured history for its sync mode. It is
#: the interval production has run on, so "no data" can never be used to
#: unlock a more aggressive schedule than the one already known to work.
DEFAULT_MIN_INTERVAL_MINUTES = 30

#: History keys. A pull is always a full pull, whatever ``sync_mode`` says.
PULL_MODE = "full"
PUSH_MODE = "push"
RECONCILE_MODE = "reconcile"

#: The push worker never calls BIMS and normally takes seconds, so it may tick
#: far more often than a pull. It is still bounded by its own measured runs.
PUSH_ABSOLUTE_MIN_INTERVAL_MINUTES = 1
PUSH_DEFAULT_MIN_INTERVAL_MINUTES = 1

#: A reconcile pages the WHOLE Shopify catalog (tens of thousands of variants,
#: minutes to tens of minutes under Shopify's query-cost throttle) while holding
#: the tenant's push lock, so it is never allowed more often than hourly.
RECONCILE_ABSOLUTE_MIN_INTERVAL_MINUTES = 60
RECONCILE_DEFAULT_MIN_INTERVAL_MINUTES = 60

#: Only the most recent completed runs count, so one ancient outlier does
#: not pin the floor forever and a recent slowdown shows up quickly.
RECENT_RUNS_WINDOW = 10


@dataclass(frozen=True)
class IntervalFloor:
    minutes: int
    basis: str  # "measured" | "default"
    slowest_seconds: float | None
    runs_considered: int
    sync_mode: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def compute_interval_floor(
    durations: list[float],
    sync_mode: str | None = None,
    *,
    absolute_min: int = ABSOLUTE_MIN_INTERVAL_MINUTES,
    default_min: int = DEFAULT_MIN_INTERVAL_MINUTES,
) -> IntervalFloor:
    """Floor from recent run durations in seconds (empty -> conservative default)."""
    if not durations:
        return IntervalFloor(default_min, "default", None, 0, sync_mode)
    slowest = max(durations)
    minutes = max(absolute_min, math.ceil(slowest * SAFETY_FACTOR / 60))
    return IntervalFloor(minutes, "measured", slowest, len(durations), sync_mode)


def compute_interval_floor_for(durations: list[float], mode: str) -> IntervalFloor:
    """``compute_interval_floor`` with the bounds that belong to ``mode``."""
    if mode == PUSH_MODE:
        return compute_interval_floor(
            durations,
            mode,
            absolute_min=PUSH_ABSOLUTE_MIN_INTERVAL_MINUTES,
            default_min=PUSH_DEFAULT_MIN_INTERVAL_MINUTES,
        )
    if mode == RECONCILE_MODE:
        return compute_interval_floor(
            durations,
            mode,
            absolute_min=RECONCILE_ABSOLUTE_MIN_INTERVAL_MINUTES,
            default_min=RECONCILE_DEFAULT_MIN_INTERVAL_MINUTES,
        )
    return compute_interval_floor(durations, mode)


class _DurationSource(Protocol):
    async def get_recent_durations(self, tenant_id: int | None, mode: str) -> list[float]: ...


async def interval_floor_for(
    source: _DurationSource, tenant_id: int | None, sync_mode: str
) -> IntervalFloor:
    """Floor for ``sync_mode`` (``PULL_MODE`` or ``PUSH_MODE``). A tenant that
    does not exist yet (creation) has no history and gets the default."""
    durations = [] if tenant_id is None else await source.get_recent_durations(tenant_id, sync_mode)
    return compute_interval_floor_for(durations, sync_mode)


def rejection_detail(
    floor: IntervalFloor, requested_minutes: int, field: str = "sync_interval_minutes"
) -> dict:
    """Body of the 422 returned for an interval below the floor."""
    is_push = floor.sync_mode == PUSH_MODE
    is_reconcile = floor.sync_mode == RECONCILE_MODE
    runs = (
        "push runs"
        if is_push
        else "reconcile runs" if is_reconcile else f"{floor.sync_mode} syncs"
    )
    if floor.basis == "measured":
        evidence = (
            f"The slowest of the last {floor.runs_considered} completed "
            f"{runs} took {floor.slowest_seconds:g} seconds; "
            f"{SAFETY_FACTOR}x that, rounded up, is {floor.minutes} minutes."
        )
    else:
        history = (
            "push history"
            if is_push
            else "reconcile history" if is_reconcile else f"sync history for {floor.sync_mode} mode"
        )
        evidence = (
            f"There is no measured {history} yet, so the "
            f"conservative default of {floor.minutes} minutes applies until runs are measured."
        )
    return {
        "message": (
            f"{field}={requested_minutes} is below the minimum safe interval "
            f"of {floor.minutes} minutes. {evidence}"
        ),
        "requested_minutes": requested_minutes,
        "min_safe_minutes": floor.minutes,
        "slowest_recent_seconds": floor.slowest_seconds,
        "runs_considered": floor.runs_considered,
        "basis": floor.basis,
        "safety_factor": SAFETY_FACTOR,
        "sync_mode": floor.sync_mode,
    }
