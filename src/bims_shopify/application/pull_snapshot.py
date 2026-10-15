"""Use case: PULL worker. Refresh the persisted BIMS catalog snapshot.

The pull takes as long as BIMS takes. It never touches Shopify inventory and
never replaces a good snapshot with a suspect one: a pull that BIMS itself
reports as incomplete, or that returns far fewer rows than the snapshot it
would replace, is REJECTED (recorded, previous snapshot kept).
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from bims_shopify.application.sync_inventory import ProgressFn, _emit_progress
from bims_shopify.domain.snapshot import CatalogPull, SnapshotMeta
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger
from bims_shopify.ports.erp import ERPPort
from bims_shopify.ports.snapshot import SnapshotStore

logger = get_logger(__name__)

REJECT_INCOMPLETE = "incomplete"
REJECT_TOO_FEW_ROWS = "too_few_rows"
REJECT_TOO_FEW_STOCK_ROWS = "too_few_stock_rows"

#: Float slack so a drop of exactly the threshold (e.g. 100 -> 70 at 30%) is accepted.
_EPSILON = 1e-9

#: ``(tenant, pull) -> stats``; raised errors never block the snapshot.
StatsFn = Callable[[Tenant, CatalogPull], Awaitable[dict[str, Any] | None]]


@dataclass
class PullResult:
    status: str  # "ok" | "rejected"
    meta: SnapshotMeta
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    previous: SnapshotMeta | None = None
    stats: dict[str, Any] | None = None
    stats_error: str | None = None


def _drop_exceeds(new: int, previous: int, max_drop_ratio: float) -> bool:
    return (previous - new) > max_drop_ratio * previous + _EPSILON


def check_sanity(
    tenant: Tenant, pull: CatalogPull, previous: SnapshotMeta | None
) -> tuple[str, dict[str, Any]] | None:
    """Why this pull must NOT replace the current snapshot, or ``None``.

    * ``complete is False``: BIMS reported more rows than it delivered (or the
      pager stopped abnormally). ``complete is None`` (no count sent) is not a
      rejection; the size check below still applies.
    * far fewer rows (or stock rows) than the previous snapshot: the same full
      pull has returned 33,705 and 11,355 rows hours apart.
    """
    if pull.complete is False:
        return REJECT_INCOMPLETE, {
            "rows": len(pull.rows),
            "reported_count": pull.reported_count,
            "stop_reason": pull.stop_reason,
            "warnings": pull.warnings,
        }
    if previous is None:
        return None
    max_drop = tenant.pull_max_drop_ratio
    for reason, new, old, label in (
        (REJECT_TOO_FEW_ROWS, len(pull.rows), previous.rows_pulled, "rows"),
        (REJECT_TOO_FEW_STOCK_ROWS, len(pull.stock), previous.stock_rows, "stock_rows"),
    ):
        if old > 0 and _drop_exceeds(new, old, max_drop):
            return reason, {
                label: new,
                f"previous_{label}": old,
                "drop_ratio": 1 - new / old,
                "max_drop_ratio": max_drop,
                "previous_snapshot_id": previous.id,
            }
    return None


class PullCatalogSnapshot:
    def __init__(self, erp: ERPPort, snapshots: SnapshotStore, stats_fn: StatsFn | None = None) -> None:
        self._erp = erp
        self._snapshots = snapshots
        self._stats_fn = stats_fn

    async def run(self, tenant: Tenant, progress: ProgressFn | None = None) -> PullResult:
        if tenant.id is None:
            raise ValueError("tenant must be persisted before pulling a snapshot")
        started_at = datetime.now(UTC)
        clock = time.perf_counter()

        await _emit_progress(progress, 0, None, "pulling BIMS catalog and stock")
        pull = await self._erp.pull_catalog(tenant)
        pull_seconds = time.perf_counter() - clock
        await _emit_progress(
            progress, len(pull.rows), None, f"pulled {len(pull.rows)} products from BIMS"
        )

        previous = await self._snapshots.get_current_meta(tenant.id)
        verdict = check_sanity(tenant, pull, previous)
        if verdict is not None:
            reason, detail = verdict
            finished_at = datetime.now(UTC)
            pull.timings = {**pull.timings, "total_seconds": time.perf_counter() - clock}
            meta = await self._snapshots.record_rejected(
                tenant.id,
                pull,
                started_at=started_at,
                finished_at=finished_at,
                rejection={"reason": reason, **detail},
            )
            logger.error(
                "catalog_pull_rejected",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                reason=reason,
                **{k: v for k, v in detail.items() if k != "warnings"},
            )
            return PullResult("rejected", meta, reason, detail, previous)

        write_started = time.perf_counter()
        staged = await self._snapshots.stage(tenant.id, pull, started_at)
        write_seconds = time.perf_counter() - write_started
        meta = await self._snapshots.promote(
            staged.id,
            finished_at=datetime.now(UTC),
            extra_timings={
                "pull_seconds": pull_seconds,
                "write_seconds": write_seconds,
                "total_seconds": time.perf_counter() - clock,
            },
        )
        await _emit_progress(progress, len(pull.rows), None, "snapshot written")

        stats: dict[str, Any] | None = None
        stats_error: str | None = None
        if self._stats_fn is not None:
            try:
                stats = await self._stats_fn(tenant, pull)
            except Exception as exc:  # stats must never cost us the snapshot
                stats_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "catalog_pull_stats_failed",
                    tenant_slug=tenant.slug,
                    tenant_id=tenant.id,
                    error=stats_error,
                )
            if stats is not None:
                await self._snapshots.set_stats(meta.id, stats)
                meta.stats = stats
        return PullResult("ok", meta, previous=previous, stats=stats, stats_error=stats_error)
