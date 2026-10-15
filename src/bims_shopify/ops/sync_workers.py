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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.erp_adapter import BIMSERPAdapter
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.snapshot_repository import SqlAlchemySnapshotRepository
from bims_shopify.adapters.persistence.sync_state_repository import SqlAlchemySyncStateRepository
from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.application.pull_snapshot import PullCatalogSnapshot
from bims_shopify.application.sync_inventory import ProgressFn
from bims_shopify.application.sync_interval import PULL_MODE
from bims_shopify.domain.snapshot import CatalogPull
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
