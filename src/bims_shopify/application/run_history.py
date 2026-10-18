"""Keys of the per-tenant run-duration history (``sync_states.sync_duration_history``).

There is no scheduler interval to protect any more (the pull loop is paced by its own
duration and a minimum gap, the write cycle by new snapshots), so the history is no longer
a floor for a setting. It is kept as the evidence behind two numbers an operator needs: how
long a pull takes and how long a write cycle takes, and so whether a cycle per snapshot is
sustainable (``GET /sync/{slug}/status``).

Two workers, two histories: they must never share one, a cycle taking minutes would hide
how long a pull takes.
"""
from __future__ import annotations

#: History keys. A pull is always a full pull. The stored values predate the rename of
#: the write path and are kept so existing history stays readable.
PULL_MODE = "full"
CYCLE_MODE = "reconcile"

#: Only the most recent completed runs count, so one ancient outlier does not
#: pin the figures forever and a recent slowdown shows up quickly.
RECENT_RUNS_WINDOW = 10
