"""The self-healing stages of the scheduled cycle: create what is missing, publish what qualifies.

The cycle (see ``ops.sync_workers.do_cycle``) is: pull -> [create missing] ->
push stock -> [publish]. This module holds the two optional stages' decisions
and Shopify writes; ``sync_workers`` owns the database bookkeeping around them.

Both stages judge from the persisted snapshot only (they never call BIMS) and
reuse the manual tools' logic instead of keeping a second implementation:

* create: ``apply_import`` (a wholly new product via ``productSet``; a size
  added in BIMS to a product already in the store is added to THAT product via
  ``productVariantsBulkCreate``; a size the product already has is a conflict,
  recorded and never forced). Products are created as DRAFT: publishing is the
  next stage's call.
* publish: ``compute_publish_plan`` / ``apply_publish`` with their gates
  unchanged (images unless the tenant opts out, confirmed stock, every SKU
  known to BIMS).

A healthy steady state must cost (almost) nothing. The create stage keeps an
index of the SKUs Shopify held at its last scan; while every SKU BIMS has in
stock is in that index there is nothing to scan and nothing to write. The
index is rescanned every ``SKU_INDEX_MAX_AGE`` so a product deleted in the
Shopify admin comes back.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from bims_shopify.adapters.shopify.client import ShopifyClient
from bims_shopify.domain.snapshot import CatalogSnapshot
from bims_shopify.domain.tenant import Tenant
from bims_shopify.ops.catalog import (
    ONLINE_STORE_PUBLICATION_NAME,
    ProductGroup,
    apply_import,
    apply_publish,
    build_publish_report,
    catalog_row_to_raw,
    compute_publish_plan,
    fetch_existing_index,
    filter_and_dedupe_rows,
    group_rows,
    group_skus,
    has_stock,
)

#: Most product groups the create stage writes in one cycle; the rest wait for
#: the next one. Bounds the Shopify write cost of a single tick.
#: Override per tenant with ``field_mappings["max_create_per_cycle"]``.
DEFAULT_MAX_CREATE_PER_CYCLE = 300
#: Refuse to create when more than this fraction of the product groups BIMS has
#: in stock are missing from a store that already holds a real catalog: that is
#: what a bad snapshot or a wiped store looks like, and a person should decide.
#: Override per tenant with ``field_mappings["max_create_ratio"]``.
DEFAULT_MAX_CREATE_RATIO = 0.5
#: Below this many matched SKUs the store counts as empty / being bootstrapped
#: and the ratio guard does not apply.
BOOTSTRAP_MATCHED_FLOOR = 50
#: How long the remembered Shopify SKU index is trusted before a rescan.
SKU_INDEX_MAX_AGE = timedelta(hours=6)
#: Most draft products one cycle publishes.
MAX_PUBLISH_PER_CYCLE = 500
#: Bounded sample size for titles / SKUs in a stage report.
REPORT_SAMPLE_SIZE = 20

DECLINE_SKU_FIELD = "sku_field_not_code2"
DECLINE_PUSH_GUARD = "push_guard_fired"


def sku_field_decline(tenant: Tenant) -> tuple[str, dict[str, Any]] | None:
    """The import names variants after ``code2``. A tenant whose snapshot SKU is
    another field would get products created under SKUs the stock sync never
    matches, so the stage declines instead of creating them."""
    configured = tenant.field_mappings.get("product_sku_field", "code2")
    if configured != "code2":
        return DECLINE_SKU_FIELD, {"product_sku_field": configured}
    return None


def eligible_groups(snapshot: CatalogSnapshot) -> list[ProductGroup]:
    """Product groups the import would create: enabled, e-commerce visible, with a
    ``code2``, and at least one variant with CONFIRMED stock above zero.

    A SKU absent from the stock answer is unknown, and unknown is not stock.
    """
    stock = {sku: record.quantity for sku, record in snapshot.stock.items()}
    rows, _duplicates = filter_and_dedupe_rows([catalog_row_to_raw(r) for r in snapshot.rows])
    groups = group_rows(rows).groups
    return [g for g in groups if any(has_stock(stock, v.sku) for v in g.variants)]


@dataclass
class CreateStageResult:
    status: str  # "noop" | "ok" | "needs_confirmation"
    report: dict[str, Any]
    #: The SKU index to remember, or ``None`` to leave the stored one alone.
    new_index: dict[str, Any] | None = None
    #: SKUs this run wrote to Shopify (their push baseline must be invalidated).
    written_skus: set[str] = field(default_factory=set)


def _sample(items: list[Any]) -> list[Any]:
    return items[:REPORT_SAMPLE_SIZE]


def _index_is_fresh(index: dict[str, Any] | None, now: datetime) -> bool:
    if not index or not index.get("at"):
        return False
    return now - datetime.fromisoformat(index["at"]) < SKU_INDEX_MAX_AGE


async def create_missing(
    tenant: Tenant,
    groups: list[ProductGroup],
    index: dict[str, Any] | None,
    make_storefront: Callable[[], ShopifyClient],
    *,
    now: datetime,
) -> CreateStageResult:
    """Create the groups (or just their missing variants) the store lacks.

    ``index`` is the remembered ``{"at", "skus", "parked"}`` document. With a
    fresh one and every wanted SKU in it (present, or parked as a conflict) the
    answer is ``noop`` with no Shopify call at all. Otherwise ONE variant scan
    gives the truth and only the difference is written.
    """
    eligible = len(groups)
    if _index_is_fresh(index, now):
        assert index is not None
        known = set(index.get("skus") or []) | set(index.get("parked") or [])
        if not any(group_skus(g) - known for g in groups):
            return CreateStageResult(
                "noop", {"scanned_shopify": False, "eligible_products": eligible, "missing_products": 0}
            )

    storefront = make_storefront()
    try:
        existing = await fetch_existing_index(storefront)
        present = set(existing.sku_to_product)
        wanted = {sku for g in groups for sku in group_skus(g)}
        pending = [g for g in groups if group_skus(g) - present]
        report: dict[str, Any] = {
            "scanned_shopify": True,
            "eligible_products": eligible,
            "missing_products": len(pending),
        }
        refreshed = {"at": now.isoformat(), "skus": sorted(present), "parked": []}

        if not pending:
            return CreateStageResult("noop", report, refreshed)

        max_ratio = float(tenant.field_mappings.get("max_create_ratio", DEFAULT_MAX_CREATE_RATIO))
        matched = len(present & wanted)
        if matched >= BOOTSTRAP_MATCHED_FLOOR and eligible and len(pending) / eligible > max_ratio:
            report.update(
                matched_skus=matched, ratio=round(len(pending) / eligible, 4), max_ratio=max_ratio
            )
            return CreateStageResult("needs_confirmation", report)

        cap = int(tenant.field_mappings.get("max_create_per_cycle", DEFAULT_MAX_CREATE_PER_CYCLE))
        batch = pending[:cap]
        result = await apply_import(
            storefront,
            batch,
            existing_skus=present,
            existing_index=existing,
            status="DRAFT",
            location_id=tenant.shopify_location_id,
        )
        written = {sku for e in result.created for sku in e["skus"]} | {
            sku for e in result.variants_added for sku in e["skus"]
        }
        summary = result.to_summary()
        report.update(
            created={
                "products": len(result.created),
                "variants": sum(len(e["skus"]) for e in result.created),
                "sample": _sample([e["title"] for e in result.created]),
            },
            variants_added=summary["variants_added"],
            conflicts=summary["conflicts"],
            failed={"count": len(result.failed), "sample": _sample(result.failed)},
            deferred=len(pending) - len(batch),
        )
        refreshed["skus"] = sorted(present | written)
        refreshed["parked"] = sorted({c["sku"] for c in result.conflicts})
        return CreateStageResult("ok", report, refreshed, written)
    finally:
        await storefront.aclose()


# -- publish --------------------------------------------------------------------------


@dataclass
class PublishStageResult:
    status: str  # "noop" | "ok"
    report: dict[str, Any]


async def publish_qualifying(
    tenant: Tenant,
    snapshot: CatalogSnapshot,
    make_storefront: Callable[[], ShopifyClient],
    *,
    require_images: bool = True,
) -> PublishStageResult:
    """Publish the DRAFT products that pass the existing ``publish`` gates.

    The gates are ``compute_publish_plan``'s, unchanged, fed with the snapshot's
    consolidated stock instead of a live BIMS call (a SKU the snapshot has no
    stock record for is unknown, so its product is left alone). Only DRAFT
    products are listed (one query, a handful of pages) rather than the whole
    catalog: publishing is what moves a product out of draft, and the products
    this system creates are drafts.
    """
    stock = {sku: record.quantity for sku, record in snapshot.stock.items()}
    storefront = make_storefront()
    try:
        drafts: list[dict[str, Any]] = []
        async for node in storefront.iter_all_products_with_skus(query="status:draft"):
            drafts.append(node)
        # Draft products can never be "already done", so the publication id is
        # only needed once there is something to publish.
        plan = compute_publish_plan(drafts, stock, publication_id="", require_images=require_images)
        deferred = max(0, len(plan.targets) - MAX_PUBLISH_PER_CYCLE)
        plan.targets = plan.targets[:MAX_PUBLISH_PER_CYCLE]
        report = build_publish_report(plan)
        report["require_images"] = require_images
        report["drafts_scanned"] = len(drafts)
        report["deferred"] = deferred
        if not plan.targets:
            return PublishStageResult("noop", report)

        publications = await storefront.fetch_publications()
        publication_id = next(
            (p["id"] for p in publications if p.get("name") == ONLINE_STORE_PUBLICATION_NAME), None
        )
        if publication_id is None:
            # Same refusal as the manual command: never publish without a target.
            raise RuntimeError(
                f"could not find the '{ONLINE_STORE_PUBLICATION_NAME}' sales-channel publication"
            )
        applied = await apply_publish(storefront, plan.targets, publication_ids=[publication_id])
        report["published"] = {"count": applied.processed, "sample": _sample(plan.targets)}
        report["failed"] = {"count": len(applied.failed), "sample": _sample(applied.failed)}
        return PublishStageResult("ok", report)
    finally:
        await storefront.aclose()
