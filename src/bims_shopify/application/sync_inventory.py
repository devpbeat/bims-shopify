"""Shared pieces of every stock write: the safety guard, the batch writer, the report bucket.

The decision of WHAT to write lives in ``reconcile_inventory`` (snapshot vs Shopify's real
quantities); this module holds the rules that apply to any write of quantities."""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bims_shopify.domain.product import InventoryDelta
from bims_shopify.domain.tenant import Tenant
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
FAILED_SAMPLE_SIZE = 20
BLOCKED_SAMPLE_SIZE = 20
#: Error text is truncated so one huge GraphQL error cannot bloat a job row.
FAILED_ERROR_MAX_CHARS = 300


class _Bucket:
    """A count plus a bounded sample of rows.

    ``namespace`` is the module globals holding the ``*_SAMPLE_SIZE`` constant
    (default: this module's), so another report (the reconciliation) can keep
    its own constants next to its code.
    """

    def __init__(self, limit_name: str, namespace: dict | None = None) -> None:
        self._limit_name = limit_name
        self._namespace = namespace
        self.count = 0
        self.sample: list[dict] = []

    def add(self, row: dict, n: int = 1) -> None:
        self.count += n
        # Resolve the limit at call time so the module constant stays the
        # single source of truth (and is patchable in tests).
        limits = self._namespace if self._namespace is not None else globals()
        if len(self.sample) < limits[self._limit_name]:
            self.sample.append(row)

    def as_dict(self) -> dict:
        return {"count": self.count, "sample": self.sample}


def _delta_row(delta: InventoryDelta) -> dict:
    return {"sku": delta.sku, "previous": delta.previous_stock, "new": delta.new_stock}


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


def guard_numbers(
    tenant: Tenant,
    resolved: list[InventoryDelta],
    result: NeedsConfirmation | None,
    force: bool,
) -> dict:
    """The exact inputs ``check_safety_guard`` evaluated, for the report."""
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


def check_safety_guard(
    tenant: Tenant, resolved: list[InventoryDelta]
) -> NeedsConfirmation | None:
    """Abort the push (before any Shopify write) if it looks unsafe.

    Enforced even when dry_run=False. Thresholds are tenant-configurable
    via ``field_mappings``, defaulting to 20% zeroed / 500 changed. The rule for
    every path that writes quantities.
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


async def push_in_batches(
    storefront: StorefrontPort,
    tenant: Tenant,
    resolved: list[InventoryDelta],
    *,
    updated: _Bucket,
    failed: _Bucket,
    progress: ProgressFn | None = None,
) -> None:
    """Write ``resolved`` to Shopify in batches of ``INVENTORY_PUSH_BATCH_SIZE``.

    Records every written row in ``updated``; when a batch raises, records its
    rows in ``failed`` and re-raises (failure semantics are the caller's).
    """
    total_batches = max(1, -(-len(resolved) // INVENTORY_PUSH_BATCH_SIZE)) if resolved else 0
    for i in range(0, len(resolved), INVENTORY_PUSH_BATCH_SIZE):
        batch = resolved[i : i + INVENTORY_PUSH_BATCH_SIZE]
        if batch:
            try:
                await storefront.set_inventory_quantities(tenant, batch)
            except Exception as exc:
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
