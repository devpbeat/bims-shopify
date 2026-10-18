"""Use case: compare Shopify's REAL inventory with the snapshot and correct it.

This is the ONLY way stock is written. It reads the quantity Shopify actually has at the
tenant's location, and writes every SKU that differs from the snapshot (BIMS is the
source of truth, Shopify is the mirror, and every cycle re-verifies the mirror). It
used to be the slow, occasional second path next to a cheap push that diffed against a
stored "what we last sent" baseline; that baseline could drift away from Shopify and
freeze a SKU forever, so it no longer exists. How many SKUs differed, and by how much,
is the report's headline: a healthy system reconciles to roughly zero, and the number is
the per-cycle health metric.

Rules that hold for every write:

* the snapshot is consumed through the ``ProductLister`` port (the stock in it
  was consolidated with ``consolidate_sellable_stock`` when it was pulled; this
  module has no second consolidation);
* absent BIMS stock is UNKNOWN (``stock is None``) and is never written; a SKU BIMS
  reports as 0 IS written as 0;
* the safety guard (``check_safety_guard``) runs before any write, and the batch
  writer is ``push_in_batches``.

Two Shopify facts this pass must not blur: a variant with NO inventory level at
the location is not a variant holding zero (writing a zero there would invent
a level that was never stocked), and the push writes integers, so the
comparison is made on the integer that would be written.
"""
from __future__ import annotations

from dataclasses import dataclass

from bims_shopify.application.sync_inventory import (
    NeedsConfirmation,
    ProgressFn,
    _Bucket,
    _emit_progress,
    check_safety_guard,
    guard_numbers,
    push_in_batches,
)
from bims_shopify.domain.product import InventoryDelta
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ports.erp import ProductLister
from bims_shopify.ports.storefront import StorefrontPort

# Bounded samples, same convention as sync_inventory / ops.catalog: the report
# carries the full COUNT of every bucket and only a few example rows.
DRIFT_SAMPLE_SIZE = 20
NO_LEVEL_SAMPLE_SIZE = 20
NOT_IN_SHOPIFY_SAMPLE_SIZE = 20
UPDATED_SAMPLE_SIZE = 20
FAILED_SAMPLE_SIZE = 20
BLOCKED_SAMPLE_SIZE = 20

#: Progress is reported every this many Shopify variants read.
READ_PROGRESS_EVERY = 1000


def _bucket(limit_name: str) -> _Bucket:
    return _Bucket(limit_name, globals())


@dataclass
class ReconcilePlan:
    """What a comparison found; nothing has been written yet."""

    report: dict
    #: SKUs to write, already resolved to their inventory item ids.
    to_push: list[InventoryDelta]


class ReconcileInventory:
    def __init__(self, erp: ProductLister, storefront: StorefrontPort) -> None:
        self._erp = erp
        self._storefront = storefront

    async def plan(self, tenant: Tenant, progress: ProgressFn | None = None) -> ReconcilePlan:
        """Read Shopify's real quantities and classify every SKU. Writes nothing."""
        products = await self._erp.list_products(tenant, since=None)
        snapshot_by_sku = {p.sku: p for p in products}

        shopify: dict[str, dict] = {}
        async for row in self._storefront.iter_variant_inventory(tenant):
            shopify[row["sku"]] = row  # duplicate SKUs: the last wins, as in the sku map
            if len(shopify) % READ_PROGRESS_EVERY == 0:
                await _emit_progress(
                    progress, len(shopify), None, f"read {len(shopify)} Shopify variants"
                )
        await _emit_progress(
            progress, len(shopify), None, f"read {len(shopify)} Shopify variants"
        )

        drift = _bucket("DRIFT_SAMPLE_SIZE")
        no_level = _bucket("NO_LEVEL_SAMPLE_SIZE")
        not_in_shopify = _bucket("NOT_IN_SHOPIFY_SAMPLE_SIZE")
        total_abs_delta = 0
        max_abs_delta = 0
        no_level_pushed = 0
        no_level_skipped_zero = 0
        unknown_stock = 0
        in_sync = 0
        to_push: list[InventoryDelta] = []

        for sku, row in shopify.items():
            product = snapshot_by_sku.get(sku)
            if product is None or product.stock is None:
                unknown_stock += 1  # UNKNOWN, never zero: Shopify's number is left alone
                continue
            stock = product.stock
            target = int(stock)  # what a push would actually write
            available = row["available"]
            if available is None:
                # No inventory level at the location: not the same as zero.
                if target == 0:
                    no_level_skipped_zero += 1
                    no_level.add({"sku": sku, "shopify": None, "snapshot": stock, "action": "skip"})
                    continue
                no_level_pushed += 1
                no_level.add({"sku": sku, "shopify": None, "snapshot": stock, "action": "push"})
                to_push.append(self._delta(sku, None, stock, row))
                continue
            if available == target:
                in_sync += 1
                continue
            delta = target - available
            total_abs_delta += abs(delta)
            max_abs_delta = max(max_abs_delta, abs(delta))
            drift.add({"sku": sku, "shopify": available, "snapshot": stock, "delta": delta})
            to_push.append(self._delta(sku, float(available), stock, row))

        for sku, product in snapshot_by_sku.items():
            if sku not in shopify:
                not_in_shopify.add({"sku": sku, "bims_quantity": product.stock})

        report = {
            "mode": "reconcile",
            "scanned": {"snapshot": len(snapshot_by_sku), "shopify": len(shopify)},
            "in_sync": {"count": in_sync},
            # The headline: SKUs whose real Shopify quantity differs from the snapshot.
            "drift": {
                **drift.as_dict(),
                "total_abs_delta": total_abs_delta,
                "max_abs_delta": max_abs_delta,
            },
            # Variants with no inventory level at the location (never a zero).
            "no_level": {
                **no_level.as_dict(),
                "pushed": no_level_pushed,
                "skipped_zero": no_level_skipped_zero,
            },
            "unknown_stock": {"count": unknown_stock},
            "not_in_shopify": not_in_shopify.as_dict(),
            "updated": {"count": 0, "sample": []},
            "failed": {"count": 0, "sample": []},
            "blocked_by_guard": {"count": 0, "sample": []},
            "guard": None,
        }
        return ReconcilePlan(report=report, to_push=to_push)

    @staticmethod
    def _delta(sku: str, previous: float | None, stock: float, row: dict) -> InventoryDelta:
        return InventoryDelta(
            sku=sku,
            previous_stock=previous,
            new_stock=stock,
            variant_inventory_item_id=row["inventory_item_id"],
        )

    async def apply(
        self,
        plan: ReconcilePlan,
        tenant: Tenant,
        *,
        dry_run: bool = False,
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> list[InventoryDelta] | NeedsConfirmation:
        """Run the safety guard over the plan and write it.

        ``force`` is the operator override of the guard. A dry run evaluates the guard and returns what it would write.
        """
        report = plan.report
        resolved = plan.to_push
        guard_result = None if force else check_safety_guard(tenant, resolved)
        report["guard"] = guard_numbers(tenant, resolved, guard_result, force)
        if guard_result is not None:
            blocked = _bucket("BLOCKED_SAMPLE_SIZE")
            for d in resolved:
                blocked.add({"sku": d.sku, "previous": d.previous_stock, "new": d.new_stock})
            report["blocked_by_guard"] = blocked.as_dict()
            return guard_result
        if dry_run:
            return resolved

        updated = _bucket("UPDATED_SAMPLE_SIZE")
        failed = _bucket("FAILED_SAMPLE_SIZE")
        try:
            await push_in_batches(
                self._storefront, tenant, resolved, updated=updated, failed=failed, progress=progress
            )
        finally:
            report["updated"] = updated.as_dict()
            report["failed"] = failed.as_dict()
        return resolved
