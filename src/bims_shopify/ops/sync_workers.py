"""Composition of the two inventory-sync workers over the persisted snapshot.

* ``do_pull``: BIMS -> our database (``PullCatalogSnapshot``). Takes as long as
  BIMS takes, never touches Shopify inventory.
* ``do_push``: our database -> Shopify (``SyncInventoryToShopify`` fed by the
  snapshot). Never calls BIMS.
* ``do_run_sync``: the one-shot ``pull now, then push now`` used by
  ``POST /sync/{slug}/run`` and the ``sync`` ops command. It is exactly those
  two functions in sequence, not a third code path.

None of these take locks: the caller (scheduler tick, HTTP endpoint, job
runner) owns serialisation, see ``bims_shopify.sync_locks``.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.adapters.snapshot_erp import SnapshotERPAdapter
from bims_shopify.application.pull_snapshot import PullCatalogSnapshot
from bims_shopify.application.push_snapshot import snapshot_decline
from bims_shopify.application.sync_interval import PULL_MODE, PUSH_MODE
from bims_shopify.application.sync_inventory import (
    DryRunReport,
    NeedsConfirmation,
    ProgressFn,
    SyncInventoryToShopify,
)
from bims_shopify.domain.snapshot import CatalogPull, SnapshotMeta
from bims_shopify.domain.tenant import Tenant
from bims_shopify.logging import get_logger
from bims_shopify.ops.catalog import (
    bims_catalog_stats,
    catalog_row_to_raw,
    reconciliation_audit_payload,
    reconciliation_diff,
)

logger = get_logger(__name__)

PULL_ERROR_PREFIX = "pull: "
PUSH_ERROR_PREFIX = "push: "

SKIP_REASON = "previous run still running; this tick was skipped, not queued"


def _now() -> datetime:
    return datetime.now(UTC)


async def shopify_variant_facts(storefront: ShopifyClient) -> tuple[dict[str, int], set[str]]:
    """Shopify's side of the catalog numbers, from ONE variant listing.

    Products are counted from the variants' parent ids (a Shopify product
    always has at least one variant), so no second product listing is needed.
    """
    product_ids: set[str] = set()
    variants = 0
    variants_with_sku = 0
    skus: set[str] = set()
    async for node in storefront.iter_all_variants():
        variants += 1
        product_id = (node.get("product") or {}).get("id")
        if product_id:
            product_ids.add(product_id)
        sku = (node.get("sku") or "").strip()
        if sku:
            variants_with_sku += 1
            skus.add(sku)
    facts = {
        "products": len(product_ids),
        "variants": variants,
        "variants_with_sku": variants_with_sku,
    }
    return facts, skus


async def compute_catalog_stats(tenant: Tenant, pull: CatalogPull) -> dict[str, Any]:
    """The catalog numbers the dashboard shows, from a pull already in hand.

    BIMS side: eligible products/variants, with stock. Shopify side: products,
    variants, matching SKUs (one read-only variant listing).
    """
    bims, bims_skus = bims_catalog_stats([catalog_row_to_raw(r) for r in pull.rows], pull.stock)
    storefront = ShopifyClient(tenant)
    try:
        shopify, shopify_skus = await shopify_variant_facts(storefront)
    finally:
        await storefront.aclose()
    reconciliation = reconciliation_diff(bims_skus, shopify_skus)
    return {"bims": bims, "shopify": shopify, "reconciliation": reconciliation}


async def record_skip(session_factory: async_sessionmaker, kind: str, tenant: Tenant) -> None:
    """Record that a scheduled ``kind`` ("pull" | "push") tick found the
    previous run of the same worker still going and did nothing.

    A skipped tick is not queued: the next interval simply tries again. It is
    recorded so a worker that never gets to run (for example a pull that takes
    longer than its interval) is visible instead of silent.
    """
    if tenant.id is None:
        return
    outcome = {"status": "skipped", "at": _now().isoformat(), "reason": SKIP_REASON, "trigger": "scheduled"}
    async with session_factory() as session:
        state = SqlAlchemySyncStateRepository(session)
        if kind == "pull":
            await state.set_last_pull_outcome(tenant.id, outcome)
            await SqlAlchemyAuditLogger(session).log(
                actor="system",
                action="snapshot.pull_skipped",
                entity="snapshot",
                tenant_id=tenant.id,
                payload={"reason": SKIP_REASON},
            )
        else:
            previous = await state.get_last_push_outcome(tenant.id)
            # Do not overwrite a meaningful last outcome (e.g. a decline) with
            # the noise of an overlapping tick; just note the skip alongside it.
            await state.set_last_push_outcome(
                tenant.id, {**(previous or {}), "last_skip": outcome}
            )
    logger.warning(
        f"{kind}_tick_skipped",
        tenant_slug=tenant.slug,
        tenant_id=tenant.id,
        reason=SKIP_REASON,
    )


async def do_pull(
    tenant: Tenant,
    session: AsyncSession,
    *,
    progress: ProgressFn | None = None,
    trigger: str = "scheduled",
) -> dict[str, Any]:
    """Refresh the tenant's catalog snapshot from BIMS.

    Returns ``{"status": "ok" | "rejected", ...}``. A failure (BIMS down, DB
    error) is recorded and re-raised; whatever the outcome, the previously
    current snapshot stays readable and untouched unless the new one lands.
    """
    if tenant.id is None:
        raise ValueError("tenant must be persisted before pulling")
    state = SqlAlchemySyncStateRepository(session)
    audit = SqlAlchemyAuditLogger(session)
    client = BIMSClient(tenant)
    clock = time.perf_counter()
    started_at = _now()
    try:
        use_case = PullCatalogSnapshot(
            BIMSERPAdapter(client),
            SqlAlchemySnapshotRepository(session),
            stats_fn=compute_catalog_stats,
        )
        try:
            result = await use_case.run(tenant, progress)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "catalog_pull_failed", tenant_slug=tenant.slug, tenant_id=tenant.id, error=error
            )
            await state.set_last_error(tenant.id, f"{PULL_ERROR_PREFIX}{error}")
            await state.set_last_pull_outcome(
                tenant.id,
                {"status": "failed", "at": _now().isoformat(), "trigger": trigger, "error": error},
            )
            await audit.log(
                actor="system",
                action="snapshot.pull_failed",
                entity="snapshot",
                tenant_id=tenant.id,
                payload={"error": error, "trigger": trigger},
            )
            raise

        duration = time.perf_counter() - clock
        # A rejected pull still took real BIMS time, so it counts toward the
        # pull interval floor like an accepted one.
        await state.record_run_duration(tenant.id, duration, PULL_MODE)
        meta = result.meta
        outcome: dict[str, Any] = {
            "status": result.status,
            "at": _now().isoformat(),
            "trigger": trigger,
            "snapshot_id": meta.id,
            "rows": meta.rows_pulled,
            "stock_rows": meta.stock_rows,
            "complete": meta.complete,
            "duration_seconds": duration,
            "started_at": started_at.isoformat(),
        }

        if result.status == "rejected":
            outcome.update(
                reason=result.reason,
                detail={k: v for k, v in result.detail.items() if k != "warnings"},
                previous_snapshot_id=result.previous.id if result.previous else None,
            )
            await state.set_last_pull_outcome(tenant.id, outcome)
            await state.set_last_error(
                tenant.id,
                f"{PULL_ERROR_PREFIX}pull rejected ({result.reason}); keeping the previous "
                "snapshot. See the snapshot.pull_rejected audit entry.",
            )
            await audit.log(
                actor="system",
                action="snapshot.pull_rejected",
                entity="snapshot",
                tenant_id=tenant.id,
                entity_id=str(meta.id),
                payload={
                    "reason": result.reason,
                    "detail": result.detail,
                    "snapshot_id": meta.id,
                    "previous_snapshot_id": result.previous.id if result.previous else None,
                    "trigger": trigger,
                },
            )
            return {
                "status": "rejected",
                "reason": result.reason,
                "detail": result.detail,
                "snapshot": meta.as_dict(),
                "previous_snapshot": result.previous.as_dict() if result.previous else None,
            }

        if result.stats_error:
            outcome["stats_error"] = result.stats_error
        await state.set_last_pull_outcome(tenant.id, outcome)
        await state.clear_last_error(tenant.id, only_prefix=PULL_ERROR_PREFIX)
        await audit.log(
            actor="system",
            action="snapshot.pull_completed",
            entity="snapshot",
            tenant_id=tenant.id,
            entity_id=str(meta.id),
            payload={
                "snapshot_id": meta.id,
                "rows": meta.rows_pulled,
                "stock_rows": meta.stock_rows,
                "pages": meta.pages,
                "complete": meta.complete,
                "duration_seconds": duration,
                "trigger": trigger,
            },
        )
        if result.stats is not None:
            # Same entry `status` writes, so the dashboard and
            # GET /sync/{slug}/reconciliation follow the pull, not a manual run.
            payload = reconciliation_audit_payload(
                result.stats["bims"], result.stats["shopify"], result.stats["reconciliation"]
            )
            await audit.log(
                actor="system",
                action="catalog.reconciliation",
                entity="catalog",
                tenant_id=tenant.id,
                payload={**payload, "source": "pull", "snapshot_id": meta.id},
            )
        logger.info(
            "catalog_pull_completed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            snapshot_id=meta.id,
            rows=meta.rows_pulled,
            duration_seconds=duration,
        )
        return {
            "status": "ok",
            "snapshot": meta.as_dict(),
            "stats": result.stats,
            "stats_error": result.stats_error,
        }
    finally:
        await client.aclose()


# -- PUSH ---------------------------------------------------------------------

#: Push outcomes that are re-evaluated every tick and must not flood the audit
#: log: only the first occurrence of an identical one is audited.
_REPEATABLE = ("declined", "needs_confirmation")


def _signature(outcome: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if not outcome:
        return None
    return (outcome.get("status"), outcome.get("reason"), outcome.get("snapshot_id"))


async def _record_push_outcome(
    state: SqlAlchemySyncStateRepository,
    tenant_id: int,
    outcome: dict[str, Any],
    previous: dict[str, Any] | None,
) -> bool:
    """Persist this tick's outcome; True when it repeats the previous one
    (same status, reason and snapshot), so the caller can skip re-auditing."""
    repeated = (
        outcome["status"] in _REPEATABLE
        and previous is not None
        and _signature(previous) == _signature(outcome)
    )
    outcome["first_seen_at"] = (
        (previous or {}).get("first_seen_at") or (previous or {}).get("at") if repeated else outcome["at"]
    )
    await state.set_last_push_outcome(tenant_id, outcome)
    return repeated


def _snapshot_info(meta: SnapshotMeta, now: datetime) -> dict[str, Any]:
    finished = meta.finished_at or meta.started_at
    return {
        "id": meta.id,
        "finished_at": finished.isoformat(),
        "age_seconds": round((now - finished).total_seconds(), 1),
        "rows": meta.rows_pulled,
        "stock_rows": meta.stock_rows,
    }


async def do_push(
    tenant: Tenant,
    session: AsyncSession,
    *,
    dry_run: bool = False,
    force: bool = False,
    progress: ProgressFn | None = None,
    trigger: str = "scheduled",
    skip_unchanged: bool = True,
) -> dict[str, Any]:
    """Push the difference between the current snapshot and what was last
    pushed to Shopify. Never calls BIMS.

    * ``push_declined``: no usable snapshot (missing, incomplete, stale). The
      reason is recorded; Shopify is not touched.
    * ``unchanged``: ``skip_unchanged`` and the previous push of this very
      snapshot succeeded, so there is nothing new to compare. Scheduled ticks
      use this to stay nearly free; the one-shot passes ``skip_unchanged=False``.
    * ``needs_confirmation``: the mass-zero / max-changed guard fired.
    * ``ok`` / dry run: same result shapes as before the split.

    ``force`` is the operator override of the guard and of the last-pushed
    baseline, exactly as before.
    """
    if tenant.id is None:
        raise ValueError("tenant must be persisted before pushing")
    state = SqlAlchemySyncStateRepository(session)
    audit = SqlAlchemyAuditLogger(session)
    snapshots = SqlAlchemySnapshotRepository(session)
    now = _now()
    meta = await snapshots.get_current_meta(tenant.id)
    previous_outcome = await state.get_last_push_outcome(tenant.id)

    verdict = snapshot_decline(meta, tenant, now)
    if verdict is not None:
        reason, detail = verdict
        outcome = {
            "status": "declined",
            "reason": reason,
            "detail": detail,
            "snapshot_id": meta.id if meta else None,
            "at": now.isoformat(),
            "trigger": trigger,
        }
        repeated = await _record_push_outcome(state, tenant.id, outcome, previous_outcome)
        log = logger.debug if repeated else logger.warning
        log("sync_push_declined", tenant_slug=tenant.slug, tenant_id=tenant.id, reason=reason, **detail)
        if not repeated:
            await audit.log(
                actor="system",
                action="sync.push_declined",
                entity="sync_run",
                tenant_id=tenant.id,
                payload={"reason": reason, "detail": detail, "trigger": trigger},
            )
        return {
            "status": "push_declined",
            "reason": reason,
            "detail": detail,
            "snapshot": meta.as_dict() if meta else None,
        }
    assert meta is not None  # a missing snapshot is a decline

    if (
        skip_unchanged
        and not dry_run
        and not force
        and previous_outcome is not None
        and previous_outcome.get("status") == "ok"
        and previous_outcome.get("snapshot_id") == meta.id
    ):
        return {"status": "unchanged", "snapshot": _snapshot_info(meta, now)}

    snapshot = await snapshots.load(meta)
    storefront = ShopifyClient(tenant)
    try:
        use_case = SyncInventoryToShopify(SnapshotERPAdapter(snapshot), storefront)
        previous = await state.get_product_hashes(tenant.id)
        # force=True ignores the stored baseline entirely: every product with
        # known stock is diffed against an empty one and re-pushed.
        previous_stocks = (
            {} if force else {sku: float(value) for sku, value in previous.items() if value}
        )
        info = _snapshot_info(meta, now)
        run_started_at = _now()
        clock = time.perf_counter()
        try:
            result = await use_case.run(
                tenant, previous_stocks, since=None, dry_run=dry_run, force=force, progress=progress
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "sync_run_failed",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                dry_run=dry_run,
                duration_seconds=time.perf_counter() - clock,
                error=error,
            )
            await state.set_last_error(tenant.id, f"{PUSH_ERROR_PREFIX}{error}")
            await state.set_last_push_outcome(
                tenant.id,
                {
                    "status": "failed",
                    "error": error,
                    "snapshot_id": meta.id,
                    "at": _now().isoformat(),
                    "trigger": trigger,
                },
            )
            await audit.log(
                actor="system",
                action="sync.error",
                entity="sync_run",
                tenant_id=tenant.id,
                payload={
                    "dry_run": dry_run,
                    "error": error,
                    "report": use_case.last_report,
                    "snapshot_id": meta.id,
                },
            )
            raise

        duration = time.perf_counter() - clock
        report = use_case.last_report or {}

        if dry_run:
            dry: DryRunReport = result  # type: ignore[assignment]
            logger.info(
                "sync_run_dry_run",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                total_products=dry.total_products,
                would_update=dry.would_update,
                skipped_unresolved_stock=dry.skipped_unresolved_stock,
                duration_seconds=duration,
            )
            await audit.log(
                actor="system",
                action="sync.dry_run",
                entity="sync_run",
                tenant_id=tenant.id,
                payload={
                    "total_products": dry.total_products,
                    "would_update": dry.would_update,
                    "skipped_unresolved_stock": dry.skipped_unresolved_stock,
                    "scanned": dry.detail.get("scanned"),
                    "mode": "full",
                    "snapshot_id": meta.id,
                },
            )
            return {"dry_run": True, "mode": "full", "report": asdict(dry), "snapshot": info}

        if isinstance(result, NeedsConfirmation):
            outcome = {
                "status": "needs_confirmation",
                "reason": result.reason,
                "snapshot_id": meta.id,
                "zero_count": result.zero_count,
                "matched_count": result.matched_count,
                "threshold": result.threshold,
                "at": _now().isoformat(),
                "trigger": trigger,
            }
            repeated = await _record_push_outcome(state, tenant.id, outcome, previous_outcome)
            log = logger.debug if repeated else logger.warning
            log(
                "sync_run_needs_confirmation",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                reason=result.reason,
                zero_count=result.zero_count,
                matched_count=result.matched_count,
                threshold=result.threshold,
                duration_seconds=duration,
            )
            if not repeated:
                await audit.log(
                    actor="system",
                    action="sync.needs_confirmation",
                    entity="sync_run",
                    tenant_id=tenant.id,
                    payload={
                        "reason": result.reason,
                        "zero_count": result.zero_count,
                        "matched_count": result.matched_count,
                        "threshold": result.threshold,
                        "report": report,
                        "snapshot_id": meta.id,
                    },
                )
            # last_sync must NOT advance: nothing was pushed.
            return {
                "mode": "full",
                "scanned": report["scanned"],
                "report": report,
                "status": "needs_confirmation",
                "reason": result.reason,
                "zero_count": result.zero_count,
                "matched_count": result.matched_count,
                "threshold": result.threshold,
                "snapshot": info,
            }

        deltas = result
        # New values WIN over the stored ones: the baseline must advance for
        # SKUs it already knows (it used to keep the old value for them).
        await state.set_product_hashes(
            tenant.id, {**previous, **{delta.sku: str(delta.new_stock) for delta in deltas}}
        )
        # last_sync only advances after the full run (including the push)
        # succeeded end-to-end.
        await state.set_last_run(tenant.id, run_started_at)
        await state.clear_last_error(tenant.id, only_prefix=PUSH_ERROR_PREFIX)
        timings = {
            "catalog_pull_seconds": meta.timings.get("catalog_pull_seconds"),
            "stock_fetch_seconds": meta.timings.get("stock_fetch_seconds"),
            "push_seconds": duration,
        }
        summary = {
            "status": "ok",
            "matched": len(deltas),
            "updated": len(deltas),
            "duration_seconds": duration,
            "ran_at": run_started_at.isoformat(),
            "mode": "full",
            "timings": timings,
            "catalog_pull": {
                "rows": meta.rows_pulled,
                "reported_count": meta.reported_count,
                "complete": meta.complete,
                "pages": meta.pages,
                "stop_reason": meta.stop_reason,
                "warnings": meta.warnings,
                "duration_seconds": meta.timings.get("catalog_pull_seconds"),
            },
            "scanned": report["scanned"],
            "report": report,
            "snapshot": info,
            "trigger": trigger,
        }
        await state.set_last_run_summary(tenant.id, summary)
        # Under its own history key: a push takes seconds, and recording it
        # with the pulls would collapse the pull interval floor.
        await state.record_run_duration(tenant.id, duration, PUSH_MODE)
        await state.set_last_push_outcome(
            tenant.id,
            {
                "status": "ok",
                "snapshot_id": meta.id,
                "updated": len(deltas),
                "duration_seconds": duration,
                "at": _now().isoformat(),
                "trigger": trigger,
            },
        )
        logger.info(
            "sync_run_completed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            matched=len(deltas),
            updated=len(deltas),
            duration_seconds=duration,
            snapshot_id=meta.id,
        )
        await audit.log(
            actor="system",
            action="sync.completed",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={
                "matched": len(deltas),
                "updated": len(deltas),
                "scanned": report["scanned"],
                "mode": "full",
                "snapshot_id": meta.id,
                "trigger": trigger,
            },
        )
        return {
            "status": "ok",
            "mode": "full",
            "synced_deltas": len(deltas),
            "timings": timings,
            "scanned": report["scanned"],
            "report": report,
            "snapshot": info,
        }
    finally:
        await storefront.aclose()


# -- ONE-SHOT -----------------------------------------------------------------


def _staged(progress: ProgressFn | None, stage: str) -> ProgressFn | None:
    """Prefix progress messages with the worker that emitted them."""
    if progress is None:
        return None

    async def forward(done: int, total: int | None, message: str) -> None:
        outcome = progress(done, total, f"[{stage}] {message}")
        if hasattr(outcome, "__await__"):
            await outcome

    return forward


async def do_run_sync(
    tenant: Tenant,
    dry_run: bool,
    session: AsyncSession,
    *,
    force: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Pull now, then push now: the operator's one-shot sync.

    The very same ``do_pull`` and ``do_push`` the scheduler ticks run, in
    sequence; the only differences are the trigger label and that the push
    always compares (``skip_unchanged=False``). A pull the sanity guard
    rejected stops here: pushing the older snapshot is not what a one-shot
    asked for, and the reason is returned instead.

    The caller holds the tenant's locks (see ``bims_shopify.sync_locks``).
    A dry run still refreshes the snapshot: that is a read of BIMS into our own
    database, not a write to Shopify.
    """
    pull = await do_pull(tenant, session, progress=_staged(progress, "pull"), trigger="manual")
    if pull["status"] != "ok":
        return {
            "status": "pull_rejected",
            "mode": "full",
            "reason": pull.get("reason"),
            "detail": pull.get("detail"),
            "pull": pull,
        }
    push = await do_push(
        tenant,
        session,
        dry_run=dry_run,
        force=force,
        progress=_staged(progress, "push"),
        trigger="manual",
        skip_unchanged=False,
    )
    return {**push, "pull": {"status": "ok", "snapshot": pull["snapshot"], "stats_error": pull.get("stats_error")}}
