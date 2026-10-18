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
from bims_shopify.application.reconcile_inventory import ReconcileInventory
from bims_shopify.application.sync_interval import PULL_MODE, PUSH_MODE, RECONCILE_MODE
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
from bims_shopify.ops.self_heal import (
    DECLINE_PUSH_GUARD,
    create_missing,
    eligible_groups,
    publish_qualifying,
    sku_field_decline,
)

logger = get_logger(__name__)

PULL_ERROR_PREFIX = "pull: "
PUSH_ERROR_PREFIX = "push: "
RECONCILE_ERROR_PREFIX = "reconcile: "

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
    """Record that a scheduled ``kind`` ("pull" | "push" | "reconcile") tick found the
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
        if kind == "reconcile":
            previous = await state.get_last_reconcile_outcome(tenant.id)
            # Keep the last real outcome (and its drift numbers) readable; the
            # overlapping tick is only noted alongside it.
            await state.set_last_reconcile_outcome(
                tenant.id, {**(previous or {}), "last_skip": outcome}
            )
        elif kind == "pull":
            prior = (await state.get_status(tenant.id))["last_pull_outcome"]
            if prior and prior.get("status") == "skipped":
                outcome["previous"] = prior.get("previous")
                outcome["consecutive_skips"] = int(prior.get("consecutive_skips", 1)) + 1
            else:
                outcome["previous"] = prior
                outcome["consecutive_skips"] = 1
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


async def last_pull_at(session_factory: async_sessionmaker, tenant: Tenant) -> datetime | None:
    """When this tenant's catalog was last pulled (accepted or rejected), for
    the scheduler to time the first pull after a restart. ``None``: never."""
    if tenant.id is None:
        return None
    async with session_factory() as session:
        meta = await SqlAlchemySnapshotRepository(session).get_current_meta(tenant.id)
        outcome = (await SqlAlchemySyncStateRepository(session).get_status(tenant.id))[
            "last_pull_outcome"
        ]
    candidates: list[datetime] = []
    if meta is not None and meta.finished_at is not None:
        candidates.append(meta.finished_at)
    if outcome and outcome.get("status") in ("ok", "rejected", "failed") and outcome.get("at"):
        candidates.append(datetime.fromisoformat(outcome["at"]))
    return max(candidates) if candidates else None


async def last_reconcile_at(session_factory: async_sessionmaker, tenant: Tenant) -> datetime | None:
    """When this tenant's reconcile last really ran (scanned Shopify), for the
    scheduler to time the first one after a restart. ``None``: never.

    A decline or a skip did not scan anything, so it does not count: a
    reconcile that was declined for a missing snapshot is still owed."""
    if tenant.id is None:
        return None
    async with session_factory() as session:
        outcome = await SqlAlchemySyncStateRepository(session).get_last_reconcile_outcome(
            tenant.id
        )
    if outcome and outcome.get("status") in ("ok", "needs_confirmation", "failed") and outcome.get("at"):
        return datetime.fromisoformat(outcome["at"])
    return None


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
    if meta is None:  # unreachable: snapshot_decline reports a missing snapshot
        raise RuntimeError("push reached the snapshot read without a snapshot")

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


# -- RECONCILE ----------------------------------------------------------------


def _drift_summary(report: dict[str, Any]) -> dict[str, Any]:
    """The drift numbers, as stored in the outcome and the audit log."""
    return dict(report["drift"])


async def do_reconcile(
    tenant: Tenant,
    session: AsyncSession,
    *,
    dry_run: bool = False,
    force: bool = False,
    progress: ProgressFn | None = None,
    trigger: str = "scheduled",
) -> dict[str, Any]:
    """Compare Shopify's REAL quantities with the current snapshot and push
    every SKU that differs, ignoring the stored baseline. Never calls BIMS.

    * ``reconcile_declined``: no usable snapshot (missing, incomplete, stale),
      judged before Shopify is read and again right before anything is written
      (reading a whole catalog takes a while). Shopify is not touched.
    * ``needs_confirmation``: the same mass-zero / max-changed guard as the
      push fired; nothing was written, the drift is still reported.
    * ``ok``: ``drift`` says how many SKUs had drifted, by how much, with a
      bounded sample. The baseline is then refreshed from what Shopify now
      holds, so the cheap incremental pushes that follow work from truth.

    ``force`` is the operator override of the guard, as for the push. The
    caller holds the tenant's push lock (see ``bims_shopify.sync_locks``).
    """
    if tenant.id is None:
        raise ValueError("tenant must be persisted before reconciling")
    state = SqlAlchemySyncStateRepository(session)
    audit = SqlAlchemyAuditLogger(session)
    snapshots = SqlAlchemySnapshotRepository(session)
    now = _now()
    meta = await snapshots.get_current_meta(tenant.id)

    async def decline(at_meta: SnapshotMeta | None, verdict: tuple[str, dict[str, Any]]) -> dict:
        reason, detail = verdict
        await state.set_last_reconcile_outcome(
            tenant.id,
            {
                "status": "declined",
                "reason": reason,
                "detail": detail,
                "snapshot_id": at_meta.id if at_meta else None,
                "at": _now().isoformat(),
                "trigger": trigger,
            },
        )
        logger.warning(
            "sync_reconcile_declined", tenant_slug=tenant.slug, tenant_id=tenant.id, reason=reason, **detail
        )
        await audit.log(
            actor="system",
            action="sync.reconcile_declined",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={"reason": reason, "detail": detail, "trigger": trigger},
        )
        return {
            "status": "reconcile_declined",
            "reason": reason,
            "detail": detail,
            "snapshot": at_meta.as_dict() if at_meta else None,
        }

    verdict = snapshot_decline(meta, tenant, now)
    if verdict is not None:
        return await decline(meta, verdict)
    if meta is None:  # unreachable: snapshot_decline reports a missing snapshot
        raise RuntimeError("reconcile reached the snapshot read without a snapshot")

    snapshot = await snapshots.load(meta)
    storefront = ShopifyClient(tenant)
    try:
        use_case = ReconcileInventory(SnapshotERPAdapter(snapshot), storefront)
        info = _snapshot_info(meta, now)
        run_started_at = _now()
        clock = time.perf_counter()
        report: dict[str, Any] = {}

        async def failed(exc: Exception) -> None:
            error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "sync_reconcile_failed",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                duration_seconds=time.perf_counter() - clock,
                error=error,
            )
            await state.set_last_error(tenant.id, f"{RECONCILE_ERROR_PREFIX}{error}")
            await state.set_last_reconcile_outcome(
                tenant.id,
                {
                    "status": "failed",
                    "error": error,
                    "snapshot_id": meta.id,
                    "drift": report.get("drift"),
                    "at": _now().isoformat(),
                    "trigger": trigger,
                },
            )
            await audit.log(
                actor="system",
                action="sync.reconcile_error",
                entity="sync_run",
                tenant_id=tenant.id,
                payload={"error": error, "snapshot_id": meta.id, "trigger": trigger},
            )

        try:
            plan = await use_case.plan(tenant, _staged(progress, "reconcile"))
        except Exception as exc:
            await failed(exc)
            raise
        report = plan.report

        # The scan takes a while: judge the snapshot again before writing.
        verdict = snapshot_decline(meta, tenant, _now())
        if verdict is not None:
            return await decline(meta, verdict)

        try:
            result = await use_case.apply(
                plan, tenant, dry_run=dry_run, force=force, progress=_staged(progress, "reconcile")
            )
        except Exception as exc:
            await failed(exc)
            raise

        duration = time.perf_counter() - clock
        drift = _drift_summary(report)
        base = {"drift": drift, "report": report, "snapshot": info, "scanned": report["scanned"]}

        if dry_run:
            # The guard is evaluated in a dry run too: its numbers are in the report.
            return {
                "dry_run": True,
                "mode": "reconcile",
                "would_update": len(plan.to_push),
                "guard_fired": isinstance(result, NeedsConfirmation),
                **base,
            }

        if isinstance(result, NeedsConfirmation):
            await state.set_last_reconcile_outcome(
                tenant.id,
                {
                    "status": "needs_confirmation",
                    "reason": result.reason,
                    "snapshot_id": meta.id,
                    "zero_count": result.zero_count,
                    "matched_count": result.matched_count,
                    "threshold": result.threshold,
                    "drift": drift,
                    "duration_seconds": duration,
                    "at": _now().isoformat(),
                    "trigger": trigger,
                },
            )
            logger.warning(
                "sync_reconcile_needs_confirmation",
                tenant_slug=tenant.slug,
                tenant_id=tenant.id,
                reason=result.reason,
                drift_count=drift["count"],
                zero_count=result.zero_count,
                matched_count=result.matched_count,
            )
            await audit.log(
                actor="system",
                action="sync.reconcile_needs_confirmation",
                entity="sync_run",
                tenant_id=tenant.id,
                payload={
                    "reason": result.reason,
                    "zero_count": result.zero_count,
                    "matched_count": result.matched_count,
                    "threshold": result.threshold,
                    "drift": drift,
                    "snapshot_id": meta.id,
                    "trigger": trigger,
                },
            )
            return {
                "status": "needs_confirmation",
                "reason": result.reason,
                "zero_count": result.zero_count,
                "matched_count": result.matched_count,
                "threshold": result.threshold,
                **base,
            }

        # Refresh the baseline from what Shopify now holds: the SKUs we just
        # wrote and the ones verified already right. Same value format as the
        # push, so its next diff starts from truth.
        previous = await state.get_product_hashes(tenant.id)
        refreshed = {
            **previous,
            **{sku: str(stock) for sku, stock in plan.in_sync.items()},
            **{d.sku: str(d.new_stock) for d in result},
        }
        await state.set_product_hashes(tenant.id, refreshed)
        await state.clear_last_error(tenant.id, only_prefix=RECONCILE_ERROR_PREFIX)
        await state.record_run_duration(tenant.id, duration, RECONCILE_MODE)
        no_level = report["no_level"]
        await state.set_last_reconcile_outcome(
            tenant.id,
            {
                "status": "ok",
                "snapshot_id": meta.id,
                "drift": drift,
                "updated": len(result),
                "in_sync": report["in_sync"]["count"],
                "no_level": {
                    "count": no_level["count"],
                    "pushed": no_level["pushed"],
                    "skipped_zero": no_level["skipped_zero"],
                },
                "unknown_stock": report["unknown_stock"]["count"],
                "not_in_shopify": report["not_in_shopify"]["count"],
                "scanned": report["scanned"],
                "duration_seconds": duration,
                "started_at": run_started_at.isoformat(),
                "at": _now().isoformat(),
                "trigger": trigger,
            },
        )
        logger.info(
            "sync_reconcile_completed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            snapshot_id=meta.id,
            drift_count=drift["count"],
            drift_total_abs_delta=drift["total_abs_delta"],
            updated=len(result),
            duration_seconds=duration,
        )
        await audit.log(
            actor="system",
            action="sync.reconcile_completed",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={
                "drift": drift,
                "updated": len(result),
                "scanned": report["scanned"],
                "snapshot_id": meta.id,
                "trigger": trigger,
            },
        )
        return {"status": "ok", "updated": len(result), **base}
    finally:
        await storefront.aclose()


# -- SELF-HEALING STAGES -----------------------------------------------------------

STAGE_CREATE = "create"
STAGE_PUBLISH = "publish"


async def _record_stage(
    state: SqlAlchemySyncStateRepository, tenant_id: int, stage: str, outcome: dict[str, Any]
) -> None:
    """Store one stage's outcome without disturbing the other stage's."""
    document = await state.get_last_heal_outcome(tenant_id) or {}
    await state.set_last_heal_outcome(tenant_id, {**document, stage: outcome})


async def _decline_stage(
    tenant: Tenant,
    state: SqlAlchemySyncStateRepository,
    audit: SqlAlchemyAuditLogger,
    stage: str,
    prior: dict[str, Any] | None,
    meta: SnapshotMeta | None,
    verdict: tuple[str, dict[str, Any]],
    trigger: str,
) -> dict[str, Any]:
    """Record that a stage refused to act, once: an identical refusal tick after
    tick is neither re-stored nor re-audited (same rule as the push)."""
    reason, detail = verdict
    snapshot_id = meta.id if meta else None
    repeated = (
        prior is not None
        and prior.get("status") == "declined"
        and prior.get("reason") == reason
        and prior.get("snapshot_id") == snapshot_id
    )
    if not repeated:
        await _record_stage(
            state,
            tenant.id,
            stage,
            {
                "status": "declined",
                "reason": reason,
                "detail": detail,
                "snapshot_id": snapshot_id,
                "at": _now().isoformat(),
                "trigger": trigger,
            },
        )
        logger.warning(
            f"sync_{stage}_declined", tenant_slug=tenant.slug, tenant_id=tenant.id, reason=reason
        )
        await audit.log(
            actor="system",
            action=f"sync.{stage}_declined",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={"reason": reason, "detail": detail, "trigger": trigger},
        )
    return {"status": "declined", "reason": reason, "detail": detail}


async def _stage_verdict(
    tenant: Tenant,
    state: SqlAlchemySyncStateRepository,
    meta: SnapshotMeta | None,
    now: datetime,
    *,
    sku_field: bool = False,
) -> tuple[str, dict[str, Any]] | None:
    """Why a catalog-writing stage must not act now, or ``None`` when it may.

    The same snapshot rules the push applies (missing, incomplete, stale), plus:
    the push's mass-zero / max-changed guard having fired on this very
    snapshot (a snapshot that looks wrong must not create or publish anything),
    and, for the create stage, a SKU field the import cannot name variants after.
    """
    verdict = snapshot_decline(meta, tenant, now)
    if verdict is None and sku_field:
        verdict = sku_field_decline(tenant)
    if verdict is None and meta is not None:
        push = await state.get_last_push_outcome(tenant.id)
        if (
            push is not None
            and push.get("status") == "needs_confirmation"
            and push.get("snapshot_id") == meta.id
        ):
            verdict = (DECLINE_PUSH_GUARD, {"snapshot_id": meta.id, "push_reason": push.get("reason")})
    return verdict


async def do_create_missing(
    tenant: Tenant,
    session: AsyncSession,
    *,
    trigger: str = "scheduled",
) -> dict[str, Any]:
    """Stage 1 of the scheduled cycle: create what BIMS has in stock and the store lacks.

    Judged from the current snapshot only; never calls BIMS.

    * ``disabled``: the tenant switched ``auto_create_missing`` off. Nothing is
      read, written or recorded.
    * ``declined``: no usable snapshot (missing, incomplete, stale), a SKU
      field the import cannot name variants after, or the push's mass-zero
      guard fired on this very snapshot (a snapshot that looks wrong creates
      nothing). Shopify is not touched.
    * ``unchanged``: this snapshot was already handled.
    * ``noop``: nothing is missing. With a fresh SKU index this costs no
      Shopify call.
    * ``needs_confirmation``: most of what BIMS has in stock is "missing" from
      a store that has a real catalog; nothing is written.
    * ``ok``: products created / variants added, conflicts recorded.

    The caller holds the tenant's push lock (the stage writes to the catalog
    the push and the reconcile read).
    """
    if tenant.id is None:
        raise ValueError("tenant must be persisted before creating products")
    if not tenant.auto_create_missing:
        return {"status": "disabled"}
    state = SqlAlchemySyncStateRepository(session)
    audit = SqlAlchemyAuditLogger(session)
    snapshots = SqlAlchemySnapshotRepository(session)
    now = _now()
    meta = await snapshots.get_current_meta(tenant.id)
    prior = (await state.get_last_heal_outcome(tenant.id) or {}).get(STAGE_CREATE)

    verdict = await _stage_verdict(tenant, state, meta, now, sku_field=True)
    if verdict is not None:
        return await _decline_stage(tenant, state, audit, STAGE_CREATE, prior, meta, verdict, trigger)
    if meta is None:  # unreachable: snapshot_decline reports a missing snapshot
        raise RuntimeError("create stage reached the snapshot read without a snapshot")

    if (
        prior is not None
        and prior.get("snapshot_id") == meta.id
        and prior.get("status") in ("ok", "noop")
        and not prior.get("deferred")
    ):
        return {"status": "unchanged", "snapshot_id": meta.id}

    snapshot = await snapshots.load(meta)
    groups = eligible_groups(snapshot)
    index = await state.get_shopify_sku_index(tenant.id)
    clock = time.perf_counter()
    try:
        result = await create_missing(
            tenant, groups, index, lambda: ShopifyClient(tenant), now=now
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.error("sync_create_failed", tenant_slug=tenant.slug, tenant_id=tenant.id, error=error)
        await _record_stage(
            state,
            tenant.id,
            STAGE_CREATE,
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
            action="sync.create_error",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={"error": error, "snapshot_id": meta.id, "trigger": trigger},
        )
        raise

    if result.new_index is not None:
        await state.set_shopify_sku_index(tenant.id, result.new_index)
    if result.written_skus:
        # The push baseline remembers a stock for SKUs that were not in Shopify
        # (the push records every delta, matched or not). A just-created variant
        # starts at zero in Shopify, so it must look NEW to the next push or it
        # would be judged "unchanged" and never receive its stock.
        baseline = await state.get_product_hashes(tenant.id)
        await state.set_product_hashes(
            tenant.id, {k: v for k, v in baseline.items() if k not in result.written_skus}
        )
    outcome = {
        "status": result.status,
        "snapshot_id": meta.id,
        "duration_seconds": time.perf_counter() - clock,
        "at": _now().isoformat(),
        "trigger": trigger,
        **result.report,
    }
    await _record_stage(state, tenant.id, STAGE_CREATE, outcome)
    wrote_or_flagged = result.status == "needs_confirmation" or any(
        result.report.get(k, {}).get("count") or result.report.get(k, {}).get("products")
        for k in ("created", "variants_added", "conflicts", "failed")
    )
    if wrote_or_flagged:
        logger.info(
            "sync_create_completed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            status=result.status,
            snapshot_id=meta.id,
        )
        await audit.log(
            actor="system",
            action=f"sync.create_{result.status}",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={"snapshot_id": meta.id, "trigger": trigger, **result.report},
        )
    return {
        "status": result.status,
        "report": result.report,
        "written_skus": len(result.written_skus),
        "snapshot_id": meta.id,
    }


async def do_publish(
    tenant: Tenant,
    session: AsyncSession,
    *,
    trigger: str = "scheduled",
    force: bool = False,
) -> dict[str, Any]:
    """Stage 3 of the scheduled cycle: publish the drafts that pass the publish gates.

    The gates are the manual ``publish`` command's, unchanged (see
    ``ops.self_heal.publish_qualifying``); the stock is the snapshot's.

    * ``disabled``: the tenant switched ``auto_publish`` off. Nothing is read,
      written or recorded.
    * ``declined``: same refusals as the create stage (no usable snapshot, or
      the push guard fired on this snapshot). Shopify is not touched.
    * ``unchanged``: this snapshot was already handled and ``force`` is not set.
      The caller forces a run when a stage just created products, because new
      products are drafts waiting for exactly this.
    * ``noop``: no draft qualifies. Costs the draft listing and nothing else.
    * ``ok``: drafts activated and put on the Online Store channel.

    The caller holds the tenant's push lock.
    """
    if tenant.id is None:
        raise ValueError("tenant must be persisted before publishing")
    if not tenant.auto_publish:
        return {"status": "disabled"}
    state = SqlAlchemySyncStateRepository(session)
    audit = SqlAlchemyAuditLogger(session)
    snapshots = SqlAlchemySnapshotRepository(session)
    now = _now()
    meta = await snapshots.get_current_meta(tenant.id)
    prior = (await state.get_last_heal_outcome(tenant.id) or {}).get(STAGE_PUBLISH)

    verdict = await _stage_verdict(tenant, state, meta, now)
    if verdict is not None:
        return await _decline_stage(tenant, state, audit, STAGE_PUBLISH, prior, meta, verdict, trigger)
    if meta is None:  # unreachable: snapshot_decline reports a missing snapshot
        raise RuntimeError("publish stage reached the snapshot read without a snapshot")

    if (
        not force
        and prior is not None
        and prior.get("snapshot_id") == meta.id
        and prior.get("status") in ("ok", "noop")
        and not prior.get("deferred")
        and not (prior.get("failed") or {}).get("count")
    ):
        return {"status": "unchanged", "snapshot_id": meta.id}

    snapshot = await snapshots.load(meta)
    require_images = bool(tenant.field_mappings.get("publish_require_images", True))
    clock = time.perf_counter()
    try:
        result = await publish_qualifying(
            tenant, snapshot, lambda: ShopifyClient(tenant), require_images=require_images
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.error("sync_publish_failed", tenant_slug=tenant.slug, tenant_id=tenant.id, error=error)
        await _record_stage(
            state,
            tenant.id,
            STAGE_PUBLISH,
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
            action="sync.publish_error",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={"error": error, "snapshot_id": meta.id, "trigger": trigger},
        )
        raise

    await _record_stage(
        state,
        tenant.id,
        STAGE_PUBLISH,
        {
            "status": result.status,
            "snapshot_id": meta.id,
            "duration_seconds": time.perf_counter() - clock,
            "at": _now().isoformat(),
            "trigger": trigger,
            **result.report,
        },
    )
    if result.status == "ok":
        logger.info(
            "sync_publish_completed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            published=result.report["published"]["count"],
            snapshot_id=meta.id,
        )
        await audit.log(
            actor="system",
            action="sync.publish_completed",
            entity="sync_run",
            tenant_id=tenant.id,
            payload={"snapshot_id": meta.id, "trigger": trigger, **result.report},
        )
    return {"status": result.status, "report": result.report, "snapshot_id": meta.id}


# -- THE SCHEDULED CYCLE -----------------------------------------------------------


async def _isolated(stage: str, tenant: Tenant, call) -> dict[str, Any]:
    """Run an optional stage so its failure is REPORTED and never stops the cycle.

    The stage has already recorded and audited its own failure; a Shopify blip
    in the create or publish stage must not take the stock push down with it.
    """
    try:
        return await call()
    except Exception as exc:
        logger.error(
            f"scheduled_{stage}_stage_failed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _cycle_counts(create: dict[str, Any], publish: dict[str, Any]) -> dict[str, int]:
    created = (create.get("report") or {}).get("created") or {}
    added = (create.get("report") or {}).get("variants_added") or {}
    conflicts = (create.get("report") or {}).get("conflicts") or {}
    published = (publish.get("report") or {}).get("published") or {}
    return {
        "products_created": created.get("products", 0),
        "variants_added": added.get("variants", 0),
        "conflicts": conflicts.get("count", 0),
        "published": published.get("count", 0),
    }


async def do_cycle(
    tenant: Tenant, session: AsyncSession, *, trigger: str = "scheduled"
) -> dict[str, Any]:
    """The scheduled push cycle: stock, then close the gap between BIMS and the store.

    1. ``do_push``: stock, exactly as before. It runs FIRST so its mass-zero /
       max-changed guard has judged this snapshot before anything is created: a
       snapshot the guard flagged creates and publishes nothing.
    2. ``do_create_missing`` (switch ``auto_create_missing``): products and
       variants BIMS has in stock and the store lacks. When it wrote anything, a
       second push runs (never skipped as "unchanged") so the new variants
       receive their stock, and only then...
    3. ``do_publish`` (switch ``auto_publish``): drafts that pass the publish
       gates go live. A product is therefore never public while it shows zero.

    The optional stages are isolated: one failing is reported under its own key
    and never stops the others or the stock push. A failing ``do_push`` still
    raises, as it did when the push was the whole tick.

    The caller holds the tenant's push lock for the whole cycle, so a reconcile
    and a manual sync stand aside exactly as they do for a plain push. In a
    healthy steady state the stages do nothing: same snapshot, no Shopify call;
    new snapshot with nothing new, no create call and one draft listing.
    """
    push = await do_push(tenant, session, trigger=trigger)
    create = await _isolated(
        STAGE_CREATE, tenant, lambda: do_create_missing(tenant, session, trigger=trigger)
    )
    wrote = bool(create.get("written_skus"))
    push_after_create: dict[str, Any] | None = None
    if wrote:
        push_after_create = await do_push(
            tenant, session, trigger=trigger, skip_unchanged=False
        )
    publish = await _isolated(
        STAGE_PUBLISH, tenant, lambda: do_publish(tenant, session, trigger=trigger, force=wrote)
    )
    counts = _cycle_counts(create, publish)
    if any(counts.values()) or create.get("status") == "failed" or publish.get("status") == "failed":
        logger.info(
            "scheduled_cycle_healed",
            tenant_slug=tenant.slug,
            tenant_id=tenant.id,
            create=create.get("status"),
            publish=publish.get("status"),
            **counts,
        )
    return {
        "push": push,
        "create": create,
        "push_after_create": push_after_create,
        "publish": publish,
        "counts": counts,
    }


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
