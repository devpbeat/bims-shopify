"""When is a snapshot fit to write to Shopify from.

Writing from a stale or incomplete snapshot is worse than not writing: Shopify
would be overwritten with numbers BIMS no longer has. Every write cycle therefore
declines, says why, and leaves Shopify as it is.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bims_shopify.domain.snapshot import SnapshotMeta
from bims_shopify.domain.tenant import Tenant

DECLINE_MISSING = "missing"
DECLINE_INCOMPLETE = "incomplete"
DECLINE_STALE = "stale"


def snapshot_decline(
    meta: SnapshotMeta | None, tenant: Tenant, now: datetime
) -> tuple[str, dict[str, Any]] | None:
    """Why a write must not run from ``meta``, or ``None`` when it may.

    * ``missing``: no snapshot has landed yet.
    * ``incomplete``: the pull was explicitly flagged incomplete
      (``complete is False``; unknown completeness, ``None``, is usable).
      The pull worker rejects such pulls, so this guards snapshots written by
      anything else.
    * ``stale``: older than the tenant's ``snapshot_max_age_minutes``, measured
      from when the pull finished (the stock was fetched just before that).
    """
    if meta is None:
        return DECLINE_MISSING, {"message": "no catalog snapshot has been pulled yet"}
    if meta.complete is False:
        return DECLINE_INCOMPLETE, {
            "snapshot_id": meta.id,
            "reported_count": meta.reported_count,
            "rows": meta.rows_pulled,
            "stop_reason": meta.stop_reason,
        }
    finished = meta.finished_at or meta.started_at
    age = now - finished
    limit = timedelta(minutes=tenant.snapshot_max_age_minutes)
    if age > limit:
        return DECLINE_STALE, {
            "snapshot_id": meta.id,
            "finished_at": finished.isoformat(),
            "age_minutes": round(age.total_seconds() / 60, 1),
            "max_age_minutes": tenant.snapshot_max_age_minutes,
        }
    return None
