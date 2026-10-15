"""Use case: compute inventory deltas from BIMS and push them to Shopify."""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from bims_shopify.domain.product import InventoryDelta, ProductSnapshot
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ports.erp import ERPPort
from bims_shopify.ports.storefront import StorefrontPort

#: Progress callback signature shared by the CLI/job runner (see
#: ops/catalog.py): (done, total-or-None, human message). May be sync or
#: async. Used here so a background sync job can report coarse phase
#: progress (pulling BIMS, resolving variants, pushing batches) instead of
#: going silent for the full run duration.
ProgressFn = Callable[[int, int | None, str], "Awaitable[None] | None"]


async def _emit_progress(progress: ProgressFn | None, done: int, total: int | None, message: str) -> None:
    if progress is None:
        return
    outcome = progress(done, total, message)
    if inspect.isawaitable(outcome):
        await outcome

# Per-run safety guard defaults (tenant-configurable via field_mappings).
# If a run would zero out more than this fraction of matched variants, or
# change more than this many variants, abort the push entirely and surface
# "needs_confirmation" instead of writing to Shopify. Applies even when
# dry_run=False.
DEFAULT_MAX_ZERO_RATIO = 0.20
DEFAULT_MAX_CHANGED_VARIANTS = 500

# Shopify's GraphQL Admin API enforces a maximum of 250 elements for array
# input arguments (see https://shopify.dev/docs/api/usage/limits), which
# applies to the `quantities` array of inventorySetQuantities.
INVENTORY_PUSH_BATCH_SIZE = 250

# Bounded samples for the per-SKU detail report. A full run covers 26k+ SKUs,
# so the report carries the full COUNT of every bucket but only a small sample
# of rows (same convention as the *_SAMPLE_SIZE constants in ops/catalog.py).
UPDATED_SAMPLE_SIZE = 20
UNCHANGED_SAMPLE_SIZE = 10
NOT_IN_SHOPIFY_SAMPLE_SIZE = 20
NOT_IN_BIMS_SAMPLE_SIZE = 20
ZERO_STOCK_SAMPLE_SIZE = 20
FAILED_SAMPLE_SIZE = 20
BLOCKED_SAMPLE_SIZE = 20
#: Error text is truncated so one huge GraphQL error cannot bloat a job row.
FAILED_ERROR_MAX_CHARS = 300


class _Bucket:
    """A count plus a bounded sample of rows."""

    def __init__(self, limit_name: str) -> None:
        self._limit_name = limit_name
        self.count = 0
        self.sample: list[dict] = []

    def add(self, row: dict, n: int = 1) -> None:
        self.count += n
        # Resolve the limit at call time so the module constant stays the
        # single source of truth (and is patchable in tests).
        if len(self.sample) < globals()[self._limit_name]:
            self.sample.append(row)

    def as_dict(self) -> dict:
        return {"count": self.count, "sample": self.sample}


def _build_report(
    products: list[ProductSnapshot],
    sku_map: dict[str, str],
    previous: dict[str, float],
    full_scan: bool,
) -> dict:
    """Classify every scanned SKU. Pure: never touches Shopify.

    Buckets (see ``SyncInventoryToShopify.last_report``):

    * ``updated`` / ``blocked_by_guard`` / ``failed`` are filled later, once
      the push outcome is known.
    * ``unchanged``: matched, non-zero, quantity equals the last value we
      pushed (the stored watermark). It cannot prove Shopify holds that value.
    * ``zero_stock``: matched and BIMS says zero, whether or not it changes.
      Zero-and-already-zero rows appear ONLY here, never in ``unchanged``.
    * ``not_in_shopify``: any BIMS product (with or without a stock record)
      whose SKU has no Shopify variant.
    * ``not_in_bims``: Shopify SKU with no BIMS stock record. UNKNOWN, never
      zero. Only evaluated on a full pull (an incremental pull sees a subset
      of BIMS, so every other Shopify SKU would look falsely missing).
    """
    unchanged = _Bucket("UNCHANGED_SAMPLE_SIZE")
    zero_stock = _Bucket("ZERO_STOCK_SAMPLE_SIZE")
    not_in_shopify = _Bucket("NOT_IN_SHOPIFY_SAMPLE_SIZE")
    not_in_bims = _Bucket("NOT_IN_BIMS_SAMPLE_SIZE")

    bims_with_stock: set[str] = set()
    for product in products:
        sku = product.sku
        if sku not in sku_map:
            not_in_shopify.add({"sku": sku, "bims_quantity": product.stock})
            continue
        if product.stock is None:
            continue  # unknown stock: counted in not_in_bims below
        bims_with_stock.add(sku)
        prev = previous.get(sku)
        if product.stock == 0:
            zero_stock.add({"sku": sku, "previous": prev, "new": product.stock})
        elif prev == product.stock:
            unchanged.add({"sku": sku, "quantity": product.stock})

    if full_scan:
        for sku in sku_map:
            if sku not in bims_with_stock:
                not_in_bims.add({"sku": sku})
        not_in_bims_dict = not_in_bims.as_dict()
    else:
        not_in_bims_dict = {
            "count": None,
            "sample": [],
            "not_evaluated": "incremental pull sees only a subset of BIMS",
        }

    return {
        "scanned": {"bims": len(products), "shopify": len(sku_map)},
        "full_scan": full_scan,
        "mode": "full" if full_scan else "incremental",
        "updated": {"count": 0, "sample": []},
        "unchanged": unchanged.as_dict(),
        "not_in_shopify": not_in_shopify.as_dict(),
        "not_in_bims": not_in_bims_dict,
        "zero_stock": zero_stock.as_dict(),
        "failed": {"count": 0, "sample": []},
        "blocked_by_guard": {"count": 0, "sample": []},
        "guard": None,
    }


def _delta_row(delta: InventoryDelta) -> dict:
    return {"sku": delta.sku, "previous": delta.previous_stock, "new": delta.new_stock}


@dataclass
class DryRunReport:
    """Diff-only report produced when dry_run=True: no Shopify writes happen."""

    total_products: int = 0
    with_resolved_stock: int = 0
    would_update: int = 0
    skipped_missing_variant: int = 0
    skipped_unresolved_stock: int = 0
    sample_skus: list[dict] = field(default_factory=list)
    #: Per-SKU bucket breakdown (see SyncInventoryToShopify.last_report).
    detail: dict = field(default_factory=dict)


@dataclass
class NeedsConfirmation:
    """Returned instead of pushing when the per-run safety guard trips.

    No Shopify write is performed. The caller (API layer) must surface this
    clearly and mark the run status as "needs_confirmation".
    """

    reason: str
    zero_count: int
    matched_count: int
    threshold: float | int


def diff_inventory(
    previous: dict[str, float], current: list[ProductSnapshot]
) -> list[InventoryDelta]:
    """Compute stock deltas between a previous {sku: stock} map and current snapshots.

    Snapshots with ``stock is None`` (SKU absent from the ERP's stock
    response) are skipped entirely: we do not know their real stock, so we
    must never emit a delta — and never push a quantity — for them.
    """
    deltas: list[InventoryDelta] = []
    for snapshot in current:
        if snapshot.stock is None:
            continue
        prev_stock = previous.get(snapshot.sku)
        if prev_stock is None or prev_stock != snapshot.stock:
            deltas.append(
                InventoryDelta(
                    sku=snapshot.sku,
                    previous_stock=prev_stock,
                    new_stock=snapshot.stock,
                )
            )
    return deltas


class SyncInventoryToShopify:
    def __init__(self, erp: ERPPort, storefront: StorefrontPort) -> None:
        self._erp = erp
        self._storefront = storefront
        #: Per-SKU detail of the most recent run (bounded samples, full
        #: counts). Set as soon as it is known, so it is also available when
        #: the run aborts on a guard trip or a push failure. ``run`` keeps
        #: returning the same values as before; callers read this alongside.
        self.last_report: dict | None = None

    async def run(
        self,
        tenant: Tenant,
        previous_stocks: dict[str, float],
        since: datetime | None = None,
        dry_run: bool = False,
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> list[InventoryDelta] | DryRunReport | NeedsConfirmation:
        """Run the sync.

        ``force=True`` is an explicit operator override: it bypasses the
        per-run safety guard (``_check_safety_guard``) so an intentional
        full re-push (e.g. after a store wipe+rebuild where BIMS's stored
        watermark no longer reflects Shopify reality) is never blocked by
        the zero-ratio / max-changed abort. Callers are expected to also
        pass ``previous_stocks={}`` and ``since=None`` when they want a
        true full re-push; ``force`` itself only controls the guard.
        """
        products = await self._erp.list_products(tenant, since=since)
        await _emit_progress(
            progress, len(products), None, f"pulled {len(products)} products from BIMS"
        )
        deltas = diff_inventory(previous_stocks, products)

        # Resolve every SKU -> inventory_item_id in a single bulk catalog
        # listing, once per run. This replaces per-SKU `find_variant_by_sku`
        # search-index lookups, which are both slow (N Shopify requests) and
        # unreliable for DRAFT products: Shopify's search index does not
        # reliably include drafts, so a freshly rebuilt (all-draft) catalog
        # would resolve zero variants via search even though the product
        # listing (which this map is built from) sees them fine.
        sku_map = await self._storefront.build_sku_inventory_map(tenant)
        await _emit_progress(
            progress, len(sku_map), None, f"resolved {len(sku_map)} Shopify variants"
        )

        report = _build_report(products, sku_map, previous_stocks, full_scan=since is None)
        self.last_report = report

        if dry_run:
            dry = await self._build_dry_run_report(tenant, products, deltas, sku_map)
            dry.detail = report
            return dry

        resolved: list[InventoryDelta] = []
        for delta in deltas:
            inventory_item_id = sku_map.get(delta.sku)
            if inventory_item_id is None:
                continue
            resolved.append(
                InventoryDelta(
                    sku=delta.sku,
                    previous_stock=delta.previous_stock,
                    new_stock=delta.new_stock,
                    variant_inventory_item_id=inventory_item_id,
                )
            )

        guard_result: NeedsConfirmation | None = None
        if not force:
            guard_result = self._check_safety_guard(tenant, resolved)
        report["guard"] = self._guard_numbers(tenant, resolved, guard_result, force)
        if guard_result is not None:
            blocked = _Bucket("BLOCKED_SAMPLE_SIZE")
            for d in resolved:
                blocked.add(_delta_row(d))
            report["blocked_by_guard"] = blocked.as_dict()
            return guard_result

        updated = _Bucket("UPDATED_SAMPLE_SIZE")
        failed = _Bucket("FAILED_SAMPLE_SIZE")
        total_batches = max(1, -(-len(resolved) // INVENTORY_PUSH_BATCH_SIZE)) if resolved else 0
        try:
            for i in range(0, len(resolved), INVENTORY_PUSH_BATCH_SIZE):
                batch = resolved[i : i + INVENTORY_PUSH_BATCH_SIZE]
                if batch:
                    try:
                        await self._storefront.set_inventory_quantities(tenant, batch)
                    except Exception as exc:
                        # Record, then re-raise: failure semantics are unchanged.
                        error = str(exc)[:FAILED_ERROR_MAX_CHARS]
                        for d in batch:
                            failed.add({"sku": d.sku, "error": error})
                        raise
                    for d in batch:
                        updated.add(_delta_row(d))
                batch_num = i // INVENTORY_PUSH_BATCH_SIZE + 1
                await _emit_progress(
                    progress, batch_num, total_batches, f"pushed batch {batch_num}/{total_batches}"
                )
        finally:
            report["updated"] = updated.as_dict()
            report["failed"] = failed.as_dict()
        return deltas

    def _guard_numbers(
        self,
        tenant: Tenant,
        resolved: list[InventoryDelta],
        result: NeedsConfirmation | None,
        force: bool,
    ) -> dict:
        """The exact inputs ``_check_safety_guard`` evaluated, for the report."""
        changed = len(resolved)
        zero_count = sum(1 for d in resolved if d.new_stock == 0)
        return {
            "evaluated": not force,
            "forced": force,
            "fired": result is not None,
            "reason": result.reason if result else None,
            "changed_count": changed,
            "zero_count": zero_count,
            "zero_ratio": (zero_count / changed) if changed else 0.0,
            "max_zero_ratio": float(
                tenant.field_mappings.get("max_zero_ratio", DEFAULT_MAX_ZERO_RATIO)
            ),
            "max_changed_variants": int(
                tenant.field_mappings.get("max_changed_variants", DEFAULT_MAX_CHANGED_VARIANTS)
            ),
        }

    def _check_safety_guard(
        self, tenant: Tenant, resolved: list[InventoryDelta]
    ) -> NeedsConfirmation | None:
        """Abort the push (before any Shopify write) if it looks unsafe.

        Enforced even when dry_run=False. Thresholds are tenant-configurable
        via ``field_mappings``, defaulting to 20% zeroed / 500 changed.
        """
        matched_count = len(resolved)
        if matched_count == 0:
            return None

        max_zero_ratio = float(
            tenant.field_mappings.get("max_zero_ratio", DEFAULT_MAX_ZERO_RATIO)
        )
        max_changed = int(
            tenant.field_mappings.get("max_changed_variants", DEFAULT_MAX_CHANGED_VARIANTS)
        )

        zero_count = sum(1 for d in resolved if d.new_stock == 0)
        zero_ratio = zero_count / matched_count

        if zero_ratio > max_zero_ratio:
            return NeedsConfirmation(
                reason="zero_ratio_exceeded",
                zero_count=zero_count,
                matched_count=matched_count,
                threshold=max_zero_ratio,
            )
        if matched_count > max_changed:
            return NeedsConfirmation(
                reason="max_changed_exceeded",
                zero_count=zero_count,
                matched_count=matched_count,
                threshold=max_changed,
            )
        return None

    async def _build_dry_run_report(
        self,
        tenant: Tenant,
        products: list[ProductSnapshot],
        deltas: list[InventoryDelta],
        sku_map: dict[str, str],
    ) -> DryRunReport:
        """Build a diff-only report. Never calls Shopify write endpoints."""
        report = DryRunReport(
            total_products=len(products),
            with_resolved_stock=sum(1 for p in products if p.stock is not None),
            would_update=len(deltas),
            skipped_missing_variant=sum(1 for d in deltas if d.sku not in sku_map),
            skipped_unresolved_stock=sum(1 for p in products if p.stock is None),
        )
        for delta in deltas[:3]:
            report.sample_skus.append(
                {
                    "sku": delta.sku,
                    "previous_stock": delta.previous_stock,
                    "new_stock": delta.new_stock,
                }
            )
        return report
