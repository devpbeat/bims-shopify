"""Rebuild the tenant's Shopify catalog from BIMS (source of truth).

Usage::

    python -m bims_shopify.ops.catalog <tenant_slug> wipe --yes-i-mean-it
    python -m bims_shopify.ops.catalog <tenant_slug> import [--apply] [--publish] \\
        [--only-with-stock] [--limit N]
    python -m bims_shopify.ops.catalog <tenant_slug> status
    python -m bims_shopify.ops.catalog <tenant_slug> dedupe [--apply] [--force]

``wipe`` deletes every product in the tenant's Shopify store. ``import``
rebuilds the catalog from the tenant's BIMS company (``tenant.bims_company_id``):
BIMS product rows sharing a common base title (after stripping a trailing
``(SIZE)`` suffix) become variants of one Shopify product with a "Size"
option; rows without a size suffix become single-variant products.

``import`` defaults to a dry run (prints a plan, writes nothing); pass
``--apply`` to actually create products in Shopify. New products are created
as DRAFT unless ``--publish`` is passed. A product group is skipped (treated
as already imported) if ANY of its variant SKUs already exists anywhere in
the shop, not only when all of them do -- this is what makes re-running
``import`` after a partial failure idempotent instead of re-creating
products under a shifted set of "surviving" variants.

``dedupe`` finds products that are full duplicates of an earlier product
(every one of their SKUs was already seen on an earlier product, by id) and
deletes them with ``--apply``; without it, it only prints a JSON report. It
refuses to delete more than 60% of the catalog in one run unless ``--force``
is passed.

``status`` pulls both sides (BIMS-eligible rows, Shopify's live catalog) and
prints a reconciliation report: counts on each side, SKUs present in one but
not the other (with a 20-item sample), and the last import/wipe/sync
activity. It persists the counts (not the SKU samples) to ``audit_logs`` as
a ``catalog.reconciliation`` entry, which the admin API's
``GET /sync/{slug}/reconciliation`` endpoint serves back without re-pulling.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.bims.timezones import to_bims_local
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.database import create_engine_and_sessionmaker
from bims_shopify.adapters.persistence.models import AuditLogModel
from bims_shopify.adapters.persistence.sync_state_repository import (
    SqlAlchemySyncStateRepository,
)
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.adapters.shopify.client import ShopifyClient, ShopifyGraphQLError
from bims_shopify.config import get_settings
from bims_shopify.domain.tenant import Tenant

logger = logging.getLogger(__name__)

BIMS_PAGE_LIMIT = 250
STOCK_BATCH_SIZE = 200
MAX_VARIANTS_PER_PRODUCT = 100
WIPE_PROGRESS_EVERY = 50
IMPORT_PROGRESS_EVERY = 25
DEDUPE_PROGRESS_EVERY = 50
FIX_TRACKING_PROGRESS_EVERY = 50
SAMPLE_GROUP_COUNT = 5
RECONCILIATION_SAMPLE_SIZE = 20
DEDUPE_DUPLICATES_SAMPLE_SIZE = 20
DEDUPE_PARTIAL_OVERLAP_SAMPLE_SIZE = 10
DEDUPE_MAX_DELETE_RATIO = 0.6
CLEANUP_PROGRESS_EVERY = 50
CLEANUP_TARGET_SAMPLE_SIZE = 20
CLEANUP_UNKNOWN_SAMPLE_SIZE = 10
CLEANUP_MAX_TARGET_RATIO = 0.6

#: Progress callback signature shared by the CLI and the background job
#: runner: (done, total-or-None, human message). May be sync or async.
ProgressFn = Callable[[int, int | None, str], "Awaitable[None] | None"]


async def _emit_progress(progress: ProgressFn | None, done: int, total: int | None, message: str) -> None:
    if progress is None:
        return
    outcome = progress(done, total, message)
    if inspect.isawaitable(outcome):
        await outcome


def _print_progress(done: int, total: int | None, message: str) -> None:
    print(f"  ... {message}")

#: A trailing parenthetical of 1-6 chars, e.g. " (XL)", " (32)", " (M)".
_SIZE_SUFFIX_RE = re.compile(r"\s*\(([^)]{1,6})\)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")

_TRANSIENT_SHOPIFY_EXCEPTIONS: tuple[type[Exception], ...] = (
    ShopifyGraphQLError,
    httpx.TransportError,
    TimeoutError,
)


def split_size_suffix(name: str) -> tuple[str, str | None]:
    """Split a BIMS product name into ``(base_title, size)``.

    ``size`` is ``None`` when ``name`` has no trailing ``(...)`` suffix (or
    the suffix is longer than 6 chars, which is treated as part of the title
    rather than a size token).
    """
    match = _SIZE_SUFFIX_RE.search(name or "")
    if not match:
        return (name or "").strip(), None
    base = name[: match.start()].strip()
    size = match.group(1).strip()
    return base, size or None


def normalize_base_title(base_title: str) -> str:
    """Casefold + collapse whitespace so grouping is not case/spacing sensitive."""
    return _WHITESPACE_RE.sub(" ", base_title.strip().casefold())


@dataclass
class BimsRow:
    bims_id: str
    name: str
    code2: str
    sell_price: float
    main_product_id: str | None = None
    ptype_id: str | None = None


@dataclass
class VariantRow:
    sku: str
    price: int
    size: str | None
    bims_id: str


@dataclass
class ProductGroup:
    title: str
    variants: list[VariantRow] = field(default_factory=list)

    @property
    def has_size_option(self) -> bool:
        return any(v.size for v in self.variants)


@dataclass
class GroupingReport:
    groups: list[ProductGroup] = field(default_factory=list)
    duplicate_code2: list[dict[str, Any]] = field(default_factory=list)
    filtered_out: int = 0
    split_products: list[dict[str, Any]] = field(default_factory=list)
    shadowed_size_duplicates: list[dict[str, Any]] = field(default_factory=list)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "true", "t", "yes")
    return bool(value)


def filter_and_dedupe_rows(raw_rows: list[dict[str, Any]]) -> tuple[list[BimsRow], list[dict[str, Any]]]:
    """Keep enabled, e-commerce-visible rows with a non-empty ``code2``.

    Deduplicates by ``code2``: the first row wins, later ones are reported.
    """
    seen: dict[str, BimsRow] = {}
    duplicates: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not _is_truthy(raw.get("enabled", True)):
            continue
        if _is_truthy(raw.get("exclude_ecommerce", False)):
            continue
        code2 = raw.get("code2")
        if not code2:
            continue
        code2 = str(code2)
        row = BimsRow(
            bims_id=str(raw.get("id")),
            name=raw.get("name") or "",
            code2=code2,
            sell_price=float(raw.get("sell_price") or 0),
            main_product_id=raw.get("main_product_id"),
            ptype_id=raw.get("ptype_id"),
        )
        if code2 in seen:
            duplicates.append({"code2": code2, "bims_id": row.bims_id, "name": row.name})
            continue
        seen[code2] = row
    return list(seen.values()), duplicates


def _option_value_for(variant: VariantRow) -> str:
    """The Shopify option value a variant would be built with (size or 'Default')."""
    return variant.size or "Default"


def _dedupe_variants_by_option_value(
    title: str, variants: list[VariantRow]
) -> tuple[list[VariantRow], list[dict[str, Any]]]:
    """Drop variants that collide on the same option value within a product group.

    BIMS can carry duplicate article rows that share the same size token (or,
    for no-suffix rows, the same normalized title collapsing to "Default").
    Shopify's ``productSet`` rejects the whole product if two variants have
    identical ``optionValues``, so we keep the first occurrence (catalog
    order) and report the rest as shadowed duplicates instead of failing the
    entire product.
    """
    kept: list[VariantRow] = []
    kept_by_value: dict[str, VariantRow] = {}
    shadowed_by_value: dict[str, list[str]] = {}
    for variant in variants:
        value = _option_value_for(variant)
        if value not in kept_by_value:
            kept_by_value[value] = variant
            kept.append(variant)
        else:
            shadowed_by_value.setdefault(value, []).append(variant.sku)

    shadowed_entries = [
        {
            "title": title,
            "size": value,
            "kept_sku": kept_by_value[value].sku,
            "shadowed_skus": skus,
        }
        for value, skus in shadowed_by_value.items()
    ]
    return kept, shadowed_entries


def group_rows(rows: list[BimsRow]) -> GroupingReport:
    """Group BIMS rows into Shopify products, splitting oversized groups."""
    report = GroupingReport()
    order: list[str] = []
    by_base: dict[str, list[VariantRow]] = {}
    titles: dict[str, str] = {}

    for row in rows:
        base_title, size = split_size_suffix(row.name)
        key = normalize_base_title(base_title) or normalize_base_title(row.name)
        if key not in by_base:
            by_base[key] = []
            order.append(key)
            titles[key] = base_title or row.name
        by_base[key].append(
            VariantRow(sku=row.code2, price=round(row.sell_price), size=size, bims_id=row.bims_id)
        )

    for key in order:
        title = titles[key]
        variants, shadowed_entries = _dedupe_variants_by_option_value(title, by_base[key])
        report.shadowed_size_duplicates.extend(shadowed_entries)
        if len(variants) <= MAX_VARIANTS_PER_PRODUCT:
            report.groups.append(ProductGroup(title=title, variants=variants))
            continue

        chunks = [
            variants[i : i + MAX_VARIANTS_PER_PRODUCT]
            for i in range(0, len(variants), MAX_VARIANTS_PER_PRODUCT)
        ]
        report.split_products.append(
            {"title": title, "total_variants": len(variants), "parts": len(chunks)}
        )
        for index, chunk in enumerate(chunks, start=1):
            report.groups.append(ProductGroup(title=f"{title} #{index}", variants=chunk))

    return report


def _parse_since(since_iso: str | None) -> datetime | None:
    """Parse the ``since_iso`` import option into an aware UTC datetime.

    ``None``/empty means a full pull. Raises ``ValueError`` if given a naive
    ISO string -- the incremental watermark must always be unambiguous.
    """
    if not since_iso:
        return None
    parsed = datetime.fromisoformat(since_iso)
    if parsed.tzinfo is None:
        raise ValueError("since_iso must be an aware ISO datetime (with UTC offset)")
    return parsed


async def fetch_bims_catalog_rows(
    bims_client: BIMSClient,
    company_id: int,
    *,
    since: datetime | None = None,
    tenant_timezone: str = "America/Asuncion",
) -> list[dict[str, Any]]:
    """Page through BIMS ``/api/products/index.json`` (mode=simple) for a company.

    ``since``, when given (an aware UTC datetime), narrows the pull to
    products BIMS reports as created/modified after that point via the
    ``last_update`` filter (converted to BIMS local time). ``None`` means a
    full pull of the company's catalog -- the historical, always-correct
    default used by manual imports and the very first scheduled auto-import.
    """
    params: dict[str, Any] = {
        "mode": "simple",
        "company_id": company_id,
        "limit": BIMS_PAGE_LIMIT,
    }
    if since is not None:
        params["last_update"] = to_bims_local(since, tenant_timezone)

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        body = await bims_client.get(
            "/api/products/index.json",
            params={**params, "offset": offset},
        )
        page = body.get("data") or []
        for item in page:
            rows.append(item.get("Product") or item)
        if len(page) < BIMS_PAGE_LIMIT:
            return rows
        offset += BIMS_PAGE_LIMIT


async def fetch_stock_by_sku(
    bims_client: BIMSClient, skus: list[str], warehouse_ids: list[int]
) -> dict[str, float]:
    """Fetch aggregated stock per SKU, batched, clamping negative totals to 0."""
    result: dict[str, float] = {}
    unique_skus = list(dict.fromkeys(skus))
    for i in range(0, len(unique_skus), STOCK_BATCH_SIZE):
        batch = unique_skus[i : i + STOCK_BATCH_SIZE]
        response = await bims_client.stock_fenicio(
            skus=batch, warehouse_ids=warehouse_ids, request_id=f"catalog-import-{i}"
        )
        for row in (response.get("data") or {}).get("stockPorSku") or []:
            result[str(row["sku"])] = max(0.0, float(row.get("stock") or 0))
    return result


async def fetch_existing_skus(shopify_client: ShopifyClient) -> set[str]:
    """Prefetch every SKU currently in the shop, so import runs are idempotent."""
    skus: set[str] = set()
    async for variant in shopify_client.iter_all_variants():
        sku = (variant.get("sku") or "").strip()
        if sku:
            skus.add(sku)
    return skus


def group_skus(group: ProductGroup) -> set[str]:
    return {v.sku for v in group.variants}


def reconciliation_diff(bims_skus: set[str], shopify_skus: set[str]) -> dict[str, Any]:
    """Compare BIMS-eligible SKUs against Shopify SKUs, both directions.

    ``sample`` lists are capped at ``RECONCILIATION_SAMPLE_SIZE`` and sorted
    for stable output; ``count`` always reflects the full set size.
    """
    missing_in_shopify = sorted(bims_skus - shopify_skus)
    missing_in_bims = sorted(shopify_skus - bims_skus)
    matched = bims_skus & shopify_skus
    return {
        "skus_in_bims_not_in_shopify": {
            "count": len(missing_in_shopify),
            "sample": missing_in_shopify[:RECONCILIATION_SAMPLE_SIZE],
        },
        "skus_in_shopify_not_in_bims": {
            "count": len(missing_in_bims),
            "sample": missing_in_bims[:RECONCILIATION_SAMPLE_SIZE],
        },
        "matched_skus": len(matched),
    }


def _variant_inventory_fields(location_id: str) -> dict[str, Any]:
    """Fields every new variant needs to be tracked and land in inventory sync.

    Sets ``inventoryItem.tracked=true`` and ``inventoryPolicy=DENY`` (stop
    selling at 0). When a tenant location id is available, also activates the
    item at that location via ``inventoryQuantities`` (required before
    ``inventorySetQuantities`` can write stock during the regular sync).
    """
    fields: dict[str, Any] = {
        "inventoryItem": {"tracked": True},
        "inventoryPolicy": "DENY",
    }
    if location_id and location_id != "0":
        fields["inventoryQuantities"] = [
            {"locationId": location_id, "name": "available", "quantity": 0}
        ]
    else:
        logger.warning(
            "catalog.import: tenant has no shopify_location_id; skipping inventory "
            "activation (tracking still enabled, but variant will not be activated "
            "at any location until location is configured)"
        )
    return fields


def build_product_set_input(
    group: ProductGroup, *, status: str, location_id: str = ""
) -> dict[str, Any]:
    """Build the ``ProductSetInput`` payload for one product group.

    ``location_id`` should be the tenant's ``shopify_location_id``. It is used
    to enable inventory tracking and activate each variant at that location,
    so the regular inventory sync (``inventorySetQuantities``) can later write
    stock levels. See PRODUCT_SET_INPUT and INVENTORY_ITEM_INPUT docs (2025-07):
    https://shopify.dev/docs/api/admin-graphql/2025-07/input-objects/ProductSetInput
    https://shopify.dev/docs/api/admin-graphql/2025-07/input-objects/ProductVariantSetInput
    https://shopify.dev/docs/api/admin-graphql/2025-07/input-objects/InventoryItemInput
    """
    product_input: dict[str, Any] = {"title": group.title, "status": status}
    inventory_fields = _variant_inventory_fields(location_id)
    if group.has_size_option:
        sizes = [v.size or "Default" for v in group.variants]
        product_input["productOptions"] = [
            {"name": "Size", "values": [{"name": size} for size in dict.fromkeys(sizes)]}
        ]
        product_input["variants"] = [
            {
                "sku": v.sku,
                "price": str(v.price),
                "optionValues": [{"optionName": "Size", "name": v.size or "Default"}],
                **inventory_fields,
            }
            for v in group.variants
        ]
    else:
        variant = group.variants[0]
        product_input["productOptions"] = [
            {"name": "Title", "values": [{"name": "Default Title"}]}
        ]
        product_input["variants"] = [
            {
                "sku": variant.sku,
                "price": str(variant.price),
                "optionValues": [{"optionName": "Title", "name": "Default Title"}],
                **inventory_fields,
            }
        ]
    return product_input


@dataclass
class ImportResult:
    created: list[dict[str, Any]] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "created": len(self.created),
            "skipped_existing": len(self.skipped_existing),
            "failed": len(self.failed),
            "failed_details": self.failed,
        }


async def apply_import(
    shopify_client: ShopifyClient,
    groups: list[ProductGroup],
    *,
    existing_skus: set[str],
    status: str,
    location_id: str = "",
    progress: ProgressFn | None = None,
) -> ImportResult:
    """Create each product group via productSet, isolating failures per product."""
    result = ImportResult()
    for index, group in enumerate(groups, start=1):
        skus = group_skus(group)
        if skus and (skus & existing_skus):
            result.skipped_existing.append(group.title)
        else:
            product_input = build_product_set_input(group, status=status, location_id=location_id)
            try:
                product = await shopify_client.product_set(product_input)
            except _TRANSIENT_SHOPIFY_EXCEPTIONS as exc:
                result.failed.append({"title": group.title, "error": f"{type(exc).__name__}: {exc}"})
            else:
                result.created.append({"title": group.title, "product_id": (product or {}).get("id")})

        if index % IMPORT_PROGRESS_EVERY == 0:
            message = f"{index}/{len(groups)} products processed"
            print(f"  ... {message}")
            await _emit_progress(progress, index, len(groups), message)

    return result


@dataclass
class WipeResult:
    deleted: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {"deleted": self.deleted, "failed": self.failed}


async def wipe_catalog(
    shopify_client: ShopifyClient, *, progress: ProgressFn | None = None
) -> WipeResult:
    """Delete every product in the shop, isolating per-product failures."""
    result = WipeResult()
    processed = 0
    async for product in shopify_client.iter_all_products():
        processed += 1
        try:
            await shopify_client.delete_product(product["id"])
        except _TRANSIENT_SHOPIFY_EXCEPTIONS as exc:
            result.failed.append({"product_id": product["id"], "error": f"{type(exc).__name__}: {exc}"})
        else:
            result.deleted += 1
        if processed % WIPE_PROGRESS_EVERY == 0:
            message = f"{processed} products processed"
            print(f"  ... {message}")
            await _emit_progress(progress, processed, None, message)
    return result


@dataclass
class DedupePlan:
    total_products: int = 0
    kept: int = 0
    duplicates_to_delete: list[dict[str, Any]] = field(default_factory=list)
    partial_overlap: list[dict[str, Any]] = field(default_factory=list)


def compute_dedupe_plan(products: list[dict[str, Any]]) -> DedupePlan:
    """Detect duplicate products from a stable (id-ascending) product scan.

    For each product, in order: if it has at least one SKU and every one of
    its SKUs was already claimed by an earlier *kept* product, it is a full
    duplicate (mark for deletion, do not claim its SKUs). Otherwise it is
    kept and all its SKUs are claimed; if some (but not all) of its SKUs
    were already claimed, it is reported as a partial overlap (kept, not
    deleted — this usually means a legitimate distinct product that happens
    to share a shadowed/duplicate SKU with another one).

    A product with zero SKUs is always kept (nothing to compare).
    """
    plan = DedupePlan(total_products=len(products))
    claimed: set[str] = set()
    for product in products:
        skus = {s for s in (product.get("skus") or []) if s}
        product_id = product.get("id")
        title = product.get("title")
        if skus and skus.issubset(claimed):
            plan.duplicates_to_delete.append(
                {"product_id": product_id, "title": title, "skus": sorted(skus)}
            )
            continue

        plan.kept += 1
        overlap = skus & claimed
        if overlap:
            plan.partial_overlap.append(
                {
                    "product_id": product_id,
                    "title": title,
                    "skus": sorted(skus),
                    "overlapping_skus": sorted(overlap),
                }
            )
        claimed |= skus

    return plan


def build_dedupe_report(plan: DedupePlan) -> dict[str, Any]:
    return {
        "total_products": plan.total_products,
        "kept": plan.kept,
        "duplicates_to_delete": {
            "count": len(plan.duplicates_to_delete),
            "sample": plan.duplicates_to_delete[:DEDUPE_DUPLICATES_SAMPLE_SIZE],
        },
        "partial_overlap": {
            "count": len(plan.partial_overlap),
            "sample": plan.partial_overlap[:DEDUPE_PARTIAL_OVERLAP_SAMPLE_SIZE],
        },
    }


async def fetch_products_with_skus(shopify_client: ShopifyClient) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    async for product in shopify_client.iter_all_products_with_skus():
        products.append(product)
    return products


@dataclass
class DedupeApplyResult:
    deleted: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {"deleted": self.deleted, "failed": self.failed}


async def apply_dedupe(
    shopify_client: ShopifyClient,
    duplicates: list[dict[str, Any]],
    *,
    progress: ProgressFn | None = None,
) -> DedupeApplyResult:
    """Delete every product marked as a duplicate, isolating per-product failures."""
    result = DedupeApplyResult()
    for index, duplicate in enumerate(duplicates, start=1):
        try:
            await shopify_client.delete_product(duplicate["product_id"])
        except _TRANSIENT_SHOPIFY_EXCEPTIONS as exc:
            result.failed.append(
                {"product_id": duplicate["product_id"], "error": f"{type(exc).__name__}: {exc}"}
            )
        else:
            result.deleted += 1
        if index % DEDUPE_PROGRESS_EVERY == 0:
            message = f"{index}/{len(duplicates)} duplicates processed"
            print(f"  ... {message}")
            await _emit_progress(progress, index, len(duplicates), message)
    return result


@dataclass
class FixTrackingResult:
    """Result of repairing legacy variants created with tracking disabled.

    ``checked`` counts every variant scanned; ``already_ok`` those that
    already had ``inventoryItem.tracked=true``; ``fixed`` those repaired
    (tracking enabled + activated at the tenant location); ``failed`` lists
    per-product failures so one bad product doesn't abort the whole run.
    """

    checked: int = 0
    already_ok: int = 0
    fixed: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "already_ok": self.already_ok,
            "fixed": self.fixed,
            "failed": self.failed,
        }


async def apply_fix_tracking(
    shopify_client: ShopifyClient,
    tenant: Tenant,
    *,
    progress: ProgressFn | None = None,
) -> FixTrackingResult:
    """Enable tracking + activate at the tenant location for untracked variants.

    Scans every variant in the shop via ``iter_all_variants`` (paginated).
    For each with ``inventoryItem.tracked=false``, groups by product and
    repairs it via ``productVariantsBulkUpdate`` (setting
    ``inventoryItem.tracked=true`` for every untracked variant of that
    product in one call), then activates each repaired variant's inventory
    item at the tenant location via ``inventoryActivate`` so the regular
    sync can write stock to it. Idempotent: a variant already tracked is
    left untouched and counted under ``already_ok``, so a second run fixes 0.

    Docs (2025-07):
    https://shopify.dev/docs/api/admin-graphql/2025-07/mutations/productVariantsBulkUpdate
    https://shopify.dev/docs/api/admin-graphql/2025-07/mutations/inventoryActivate
    """
    result = FixTrackingResult()
    by_product: dict[str, list[dict[str, Any]]] = {}

    async for variant in shopify_client.iter_all_variants():
        result.checked += 1
        tracked = ((variant.get("inventoryItem") or {}).get("tracked"))
        if tracked:
            result.already_ok += 1
        else:
            product_id = (variant.get("product") or {}).get("id")
            if product_id:
                by_product.setdefault(product_id, []).append(variant)
        if result.checked % FIX_TRACKING_PROGRESS_EVERY == 0:
            message = f"{result.checked} variants checked"
            print(f"  ... {message}")
            await _emit_progress(progress, result.checked, None, message)

    has_location = bool(tenant.shopify_location_id) and tenant.shopify_location_id != "0"
    if not has_location:
        logger.warning(
            "ops.fix_tracking: tenant %s has no shopify_location_id; enabling tracking "
            "but skipping activation",
            tenant.slug,
        )

    products = list(by_product.items())
    for index, (product_id, variants) in enumerate(products, start=1):
        try:
            bulk_variants = [
                {"id": v["id"], "inventoryItem": {"tracked": True}} for v in variants
            ]
            await shopify_client.bulk_update_variants(product_id, bulk_variants)
            if has_location:
                for variant in variants:
                    inventory_item_id = (variant.get("inventoryItem") or {}).get("id")
                    if inventory_item_id:
                        await shopify_client.activate_inventory_item(
                            inventory_item_id, tenant.shopify_location_id
                        )
        except _TRANSIENT_SHOPIFY_EXCEPTIONS as exc:
            result.failed.append({"product_id": product_id, "error": f"{type(exc).__name__}: {exc}"})
        else:
            result.fixed += len(variants)

        if index % FIX_TRACKING_PROGRESS_EVERY == 0:
            message = f"{index}/{len(products)} products repaired"
            print(f"  ... {message}")
            await _emit_progress(progress, index, len(products), message)

    return result


@dataclass
class CleanupPlan:
    """Result of scanning the catalog for products with no BIMS stock.

    A product is a ``target`` only when every one of its variant SKUs is
    confirmed zero-stock in BIMS (aggregated across the tenant's warehouses).
    A product with at least one variant SKU that BIMS has no record of at all
    is reported as ``unknown`` and never touched -- we only ever act on
    confirmed zero-stock, never on "we don't know". A product with zero SKUs
    is also reported as ``unknown`` (nothing to confirm against BIMS).

    ``already_done`` counts zero-stock products that are already in the
    target state for ``mode="draft"`` (status already DRAFT), so re-running
    the same mode finds ~0 new targets on the second pass. ``mode="delete"``
    has no observable "already done" state (a deleted product no longer
    appears in the scan), so it is always 0 for that mode.
    """

    total_products: int = 0
    targets: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[dict[str, Any]] = field(default_factory=list)
    already_done: int = 0


def compute_cleanup_plan(
    products: list[dict[str, Any]], stock_by_sku: dict[str, float], *, mode: str
) -> CleanupPlan:
    plan = CleanupPlan(total_products=len(products))
    for product in products:
        skus = [s for s in (product.get("skus") or []) if s]
        product_id = product.get("id")
        title = product.get("title")
        status = product.get("status")

        if not skus or any(sku not in stock_by_sku for sku in skus):
            plan.unknown.append({"product_id": product_id, "title": title, "skus": skus})
            continue

        zero_stock = all(stock_by_sku.get(sku, 0.0) <= 0 for sku in skus)
        if not zero_stock:
            continue

        if mode == "draft" and status == "DRAFT":
            plan.already_done += 1
            continue

        plan.targets.append({"product_id": product_id, "title": title, "skus": skus})

    return plan


def build_cleanup_report(plan: CleanupPlan) -> dict[str, Any]:
    return {
        "total_products": plan.total_products,
        "targets": {
            "count": len(plan.targets),
            "sample": plan.targets[:CLEANUP_TARGET_SAMPLE_SIZE],
        },
        "unknown": {
            "count": len(plan.unknown),
            "sample": plan.unknown[:CLEANUP_UNKNOWN_SAMPLE_SIZE],
        },
        "already_done": plan.already_done,
    }


@dataclass
class CleanupApplyResult:
    processed: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {"processed": self.processed, "failed": self.failed}


async def apply_cleanup(
    shopify_client: ShopifyClient,
    targets: list[dict[str, Any]],
    *,
    mode: str,
    progress: ProgressFn | None = None,
) -> CleanupApplyResult:
    """Draft or delete every target product, isolating per-product failures."""
    result = CleanupApplyResult()
    for index, target in enumerate(targets, start=1):
        product_id = target["product_id"]
        try:
            if mode == "delete":
                await shopify_client.delete_product(product_id)
            else:
                await shopify_client.set_product_status(product_id, "DRAFT")
        except _TRANSIENT_SHOPIFY_EXCEPTIONS as exc:
            result.failed.append({"product_id": product_id, "error": f"{type(exc).__name__}: {exc}"})
        else:
            result.processed += 1
        if index % CLEANUP_PROGRESS_EVERY == 0:
            message = f"{index}/{len(targets)} products processed"
            print(f"  ... {message}")
            await _emit_progress(progress, index, len(targets), message)
    return result


async def _run_cleanup_no_stock(
    tenant: Tenant, args: argparse.Namespace, progress: ProgressFn | None = None
) -> dict[str, Any]:
    mode = args.mode
    if mode not in ("draft", "delete"):
        raise SystemExit(f"cleanup_no_stock: invalid mode '{mode}' (must be 'draft' or 'delete')")

    bims_client = BIMSClient(tenant)
    shopify_client = ShopifyClient(tenant)
    try:
        await _emit_progress(progress, 0, None, "scanning Shopify catalog")
        products = await fetch_products_with_skus(shopify_client)
        all_skus = [sku for product in products for sku in (product.get("skus") or []) if sku]

        await _emit_progress(progress, 0, None, "checking BIMS stock")
        stock_by_sku = await fetch_stock_by_sku(bims_client, all_skus, tenant.stock_warehouse_ids)

        plan = compute_cleanup_plan(products, stock_by_sku, mode=mode)
        report = build_cleanup_report(plan)
        report["mode"] = mode

        if not args.apply:
            report["dry_run"] = True
            return report

        target_count = len(plan.targets)
        if plan.total_products > 0 and not args.force:
            ratio = target_count / plan.total_products
            if ratio > CLEANUP_MAX_TARGET_RATIO:
                raise SystemExit(
                    f"Safety guard: cleanup_no_stock would touch {target_count}/{plan.total_products} "
                    f"products ({ratio:.0%}), exceeding the "
                    f"{CLEANUP_MAX_TARGET_RATIO:.0%} threshold. Re-run with --force to override "
                    "if this is expected."
                )

        apply_result = await apply_cleanup(shopify_client, plan.targets, mode=mode, progress=progress)
        report["apply"] = apply_result.to_summary()
        await _audit(
            tenant.id,
            "ops.cleanup_no_stock",
            {
                "mode": mode,
                "total_products": plan.total_products,
                "targets_found": target_count,
                "unknown": len(plan.unknown),
                "already_done": plan.already_done,
                "processed": apply_result.processed,
                "failed": len(apply_result.failed),
            },
        )
        return report
    finally:
        await bims_client.aclose()
        await shopify_client.aclose()


async def _run_fix_tracking(
    tenant: Tenant, args: argparse.Namespace, progress: ProgressFn | None = None
) -> dict[str, Any]:
    shopify_client = ShopifyClient(tenant)
    try:
        await _emit_progress(progress, 0, None, "scanning variants for tracking status")
        if not args.apply:
            # Dry run still has to scan the whole catalog to produce real
            # counts, it just doesn't call any mutation.
            result = FixTrackingResult()
            by_product: dict[str, int] = {}
            async for variant in shopify_client.iter_all_variants():
                result.checked += 1
                if ((variant.get("inventoryItem") or {}).get("tracked")):
                    result.already_ok += 1
                else:
                    product_id = (variant.get("product") or {}).get("id")
                    by_product[product_id] = by_product.get(product_id, 0) + 1
                if result.checked % FIX_TRACKING_PROGRESS_EVERY == 0:
                    await _emit_progress(
                        progress, result.checked, None, f"{result.checked} variants checked"
                    )
            result.fixed = sum(by_product.values())
            summary = result.to_summary()
            summary["dry_run"] = True
            summary["products_to_fix"] = len(by_product)
            return summary

        result = await apply_fix_tracking(shopify_client, tenant, progress=progress)
        summary = result.to_summary()
        await _audit(tenant.id, "ops.fix_tracking", summary)
        return summary
    finally:
        await shopify_client.aclose()


async def _load_tenant_from_db(tenant_slug: str) -> Tenant:
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with session_factory() as session:
            repo = SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))
            tenant = await repo.get_by_slug(tenant_slug)
        if tenant is None:
            raise SystemExit(f"tenant '{tenant_slug}' not found")
        return tenant
    finally:
        await engine.dispose()


async def _audit(tenant_id: int | None, action: str, payload: dict[str, Any]) -> None:
    if tenant_id is None:
        return
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with session_factory() as session:
            audit = SqlAlchemyAuditLogger(session)
            await audit.log(actor="system", action=action, entity="catalog", tenant_id=tenant_id, payload=payload)
    finally:
        await engine.dispose()


async def _run_wipe(
    tenant: Tenant, args: argparse.Namespace, progress: ProgressFn | None = None
) -> dict[str, Any]:
    if not args.yes_i_mean_it:
        raise SystemExit("wipe requires --yes-i-mean-it")

    shopify_client = ShopifyClient(tenant)
    try:
        count = await shopify_client.products_count()
        print(f"About to delete {count} product(s) from {tenant.shopify_shop_domain}")
        await _emit_progress(progress, 0, count, f"about to delete {count} product(s)")
        result = await wipe_catalog(shopify_client, progress=progress)
        summary = result.to_summary()
        await _audit(tenant.id, "catalog.wipe", summary)
        return summary
    finally:
        await shopify_client.aclose()


async def _run_import(
    tenant: Tenant, args: argparse.Namespace, progress: ProgressFn | None = None
) -> dict[str, Any]:
    since = _parse_since(getattr(args, "since_iso", None))

    bims_client = BIMSClient(tenant)
    shopify_client = ShopifyClient(tenant)
    try:
        await _emit_progress(
            progress,
            0,
            None,
            "pulling BIMS catalog (incremental)" if since is not None else "pulling BIMS catalog",
        )
        raw_rows = await fetch_bims_catalog_rows(
            bims_client,
            tenant.bims_company_id,
            since=since,
            tenant_timezone=tenant.bims_timezone,
        )
        rows, duplicates = filter_and_dedupe_rows(raw_rows)
        grouping = group_rows(rows)
        groups = grouping.groups

        if args.only_with_stock:
            all_skus = [v.sku for g in groups for v in g.variants]
            stock_by_sku = await fetch_stock_by_sku(bims_client, all_skus, tenant.stock_warehouse_ids)
            groups = [
                g for g in groups if any(stock_by_sku.get(v.sku, 0.0) > 0 for v in g.variants)
            ]

        if args.limit is not None:
            groups = groups[: args.limit]

        summary: dict[str, Any] = {
            "products": len(groups),
            "variants": sum(len(g.variants) for g in groups),
            "duplicate_code2": len(duplicates),
            "split_products": grouping.split_products,
            "shadowed_size_duplicates": {
                "count": len(grouping.shadowed_size_duplicates),
                "items": grouping.shadowed_size_duplicates,
            },
            "incremental": since is not None,
        }

        if not args.apply:
            summary["dry_run"] = True
            summary["sample"] = [
                {
                    "title": g.title,
                    "variant_count": len(g.variants),
                    "skus": [v.sku for v in g.variants][:10],
                }
                for g in groups[:SAMPLE_GROUP_COUNT]
            ]
            return summary

        await _emit_progress(progress, 0, len(groups), "pulling existing Shopify SKUs")
        existing_skus = await fetch_existing_skus(shopify_client)
        status = "ACTIVE" if args.publish else "DRAFT"
        result = await apply_import(
            shopify_client,
            groups,
            existing_skus=existing_skus,
            status=status,
            location_id=tenant.shopify_location_id,
            progress=progress,
        )
        summary["apply"] = result.to_summary()
        await _audit(tenant.id, "catalog.import", summary)
        print("Reminder: stock levels will arrive via the regular inventory sync, not this import.")
        return summary
    finally:
        await bims_client.aclose()
        await shopify_client.aclose()


async def _run_dedupe(
    tenant: Tenant, args: argparse.Namespace, progress: ProgressFn | None = None
) -> dict[str, Any]:
    shopify_client = ShopifyClient(tenant)
    try:
        await _emit_progress(progress, 0, None, "scanning products for duplicates")
        products = await fetch_products_with_skus(shopify_client)
        plan = compute_dedupe_plan(products)
        report = build_dedupe_report(plan)

        if not args.apply:
            report["dry_run"] = True
            return report

        dup_count = len(plan.duplicates_to_delete)
        if plan.total_products > 0 and not args.force:
            ratio = dup_count / plan.total_products
            if ratio > DEDUPE_MAX_DELETE_RATIO:
                raise SystemExit(
                    f"Safety guard: dedupe would delete {dup_count}/{plan.total_products} "
                    f"products ({ratio:.0%}), exceeding the "
                    f"{DEDUPE_MAX_DELETE_RATIO:.0%} threshold. Re-run with --force to override "
                    "if this is expected."
                )

        apply_result = await apply_dedupe(shopify_client, plan.duplicates_to_delete, progress=progress)
        report["apply"] = apply_result.to_summary()
        await _audit(
            tenant.id,
            "catalog.dedupe",
            {
                "total_products": plan.total_products,
                "kept": plan.kept,
                "duplicates_found": dup_count,
                "partial_overlap": len(plan.partial_overlap),
                "deleted": apply_result.deleted,
                "failed": len(apply_result.failed),
            },
        )
        return report
    finally:
        await shopify_client.aclose()


def _audit_entry_summary(entry: AuditLogModel | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {"created_at": entry.created_at.isoformat(), "payload": entry.payload}


async def _fetch_last_activity(tenant_id: int) -> dict[str, Any]:
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with session_factory() as session:
            audit = SqlAlchemyAuditLogger(session)
            last_import = await audit.get_latest(action="catalog.import", tenant_id=tenant_id)
            last_wipe = await audit.get_latest(action="catalog.wipe", tenant_id=tenant_id)
            sync_state_repo = SqlAlchemySyncStateRepository(session)
            sync_status = await sync_state_repo.get_status(tenant_id)
        return {
            "last_import": _audit_entry_summary(last_import),
            "last_wipe": _audit_entry_summary(last_wipe),
            "last_sync": {
                "last_run_at": sync_status["last_run_at"],
                "last_run_summary": sync_status["last_run_summary"],
            },
        }
    finally:
        await engine.dispose()


async def _run_status(
    tenant: Tenant, args: argparse.Namespace, progress: ProgressFn | None = None
) -> dict[str, Any]:
    bims_client = BIMSClient(tenant)
    shopify_client = ShopifyClient(tenant)
    try:
        await _emit_progress(progress, 0, None, "pulling BIMS catalog")
        raw_rows = await fetch_bims_catalog_rows(bims_client, tenant.bims_company_id)
        rows, duplicates = filter_and_dedupe_rows(raw_rows)
        grouping = group_rows(rows)
        bims_skus = {v.sku for g in grouping.groups for v in g.variants}
        await _emit_progress(progress, 1, 2, "bims pull done, pulling Shopify catalog")

        shopify_products = 0
        async for _product in shopify_client.iter_all_products():
            shopify_products += 1

        shopify_variants = 0
        shopify_variants_with_sku = 0
        shopify_skus: set[str] = set()
        async for variant in shopify_client.iter_all_variants():
            shopify_variants += 1
            sku = (variant.get("sku") or "").strip()
            if sku:
                shopify_variants_with_sku += 1
                shopify_skus.add(sku)

        await _emit_progress(progress, 2, 2, "shopify pull done")
        reconciliation = reconciliation_diff(bims_skus, shopify_skus)

        report: dict[str, Any] = {
            "bims": {
                "eligible_products": len(grouping.groups),
                "eligible_variants": len(rows),
                "duplicate_code2": len(duplicates),
            },
            "shopify": {
                "products": shopify_products,
                "variants": shopify_variants,
                "variants_with_sku": shopify_variants_with_sku,
            },
            "reconciliation": reconciliation,
            "last_activity": await _fetch_last_activity(tenant.id),
        }

        # Persist counts only (not the SKU samples) so audit_logs stays small.
        await _audit(
            tenant.id,
            "catalog.reconciliation",
            {
                "bims": report["bims"],
                "shopify": report["shopify"],
                "reconciliation": {
                    "skus_in_bims_not_in_shopify": reconciliation["skus_in_bims_not_in_shopify"][
                        "count"
                    ],
                    "skus_in_shopify_not_in_bims": reconciliation["skus_in_shopify_not_in_bims"][
                        "count"
                    ],
                    "matched_skus": reconciliation["matched_skus"],
                },
            },
        )

        return report
    finally:
        await bims_client.aclose()
        await shopify_client.aclose()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    tenant = await _load_tenant_from_db(args.tenant_slug)
    if args.command == "wipe":
        return await _run_wipe(tenant, args)
    if args.command == "status":
        return await _run_status(tenant, args)
    if args.command == "dedupe":
        return await _run_dedupe(tenant, args)
    if args.command == "fix-tracking":
        return await _run_fix_tracking(tenant, args)
    if args.command == "cleanup-no-stock":
        return await _run_cleanup_no_stock(tenant, args)
    return await _run_import(tenant, args)


# --- In-process entry points for the background JobRunner (api/ops.py) ---
#
# These accept a plain `options` dict (validated by the ops API) instead of
# an argparse.Namespace, and an already-built `tenant`/clients-less `ctx`
# (the tenant is enough here; each command builds its own BIMSClient /
# ShopifyClient the same way the CLI does). They wrap the exact same
# `_run_*` cores the CLI uses, so behavior never diverges between the two
# entry points.


async def run_status(tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None) -> dict[str, Any]:
    return await _run_status(tenant, argparse.Namespace(**options), progress=progress)


async def run_wipe(tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None) -> dict[str, Any]:
    args = argparse.Namespace(yes_i_mean_it=bool(options.get("confirm", False)))
    return await _run_wipe(tenant, args, progress=progress)


async def run_import(tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None) -> dict[str, Any]:
    # `since_iso` is opt-in and defaults to None (full pull): manual imports
    # via the ops UI/CLI stay full unless a caller explicitly passes it. The
    # auto-import scheduler is the one caller that sets it, to run
    # incrementally after the first scheduled import.
    args = argparse.Namespace(
        apply=bool(options.get("apply", False)),
        publish=bool(options.get("publish", False)),
        only_with_stock=bool(options.get("only_with_stock", False)),
        limit=options.get("limit"),
        since_iso=options.get("since_iso"),
    )
    return await _run_import(tenant, args, progress=progress)


async def run_dedupe(tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None) -> dict[str, Any]:
    args = argparse.Namespace(
        apply=bool(options.get("apply", False)),
        force=bool(options.get("force", False)),
    )
    return await _run_dedupe(tenant, args, progress=progress)


async def run_fix_tracking(
    tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None
) -> dict[str, Any]:
    args = argparse.Namespace(apply=bool(options.get("apply", False)))
    return await _run_fix_tracking(tenant, args, progress=progress)


async def run_cleanup_no_stock(
    tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None
) -> dict[str, Any]:
    args = argparse.Namespace(
        apply=bool(options.get("apply", False)),
        mode=options.get("mode", "draft"),
        force=bool(options.get("force", False)),
    )
    return await _run_cleanup_no_stock(tenant, args, progress=progress)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild a tenant's Shopify catalog from BIMS (source of truth)."
    )
    parser.add_argument("tenant_slug", help="Tenant slug to load from the database.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    wipe_parser = subparsers.add_parser("wipe", help="Delete all products in the tenant's Shopify store.")
    wipe_parser.add_argument(
        "--yes-i-mean-it",
        dest="yes_i_mean_it",
        action="store_true",
        help="Required confirmation flag; wipe refuses to run without it.",
    )

    import_parser = subparsers.add_parser("import", help="Build the catalog from BIMS.")
    import_parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the import (default: dry run, prints the plan only).",
    )
    import_parser.add_argument(
        "--publish",
        action="store_true",
        help="Create products as ACTIVE instead of the default DRAFT.",
    )
    import_parser.add_argument(
        "--only-with-stock",
        dest="only_with_stock",
        action="store_true",
        help="Only import product groups that have stock > 0 in a tenant warehouse.",
    )
    import_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N product groups (for smoke testing).",
    )
    import_parser.add_argument(
        "--since-iso",
        dest="since_iso",
        default=None,
        help=(
            "Aware ISO-8601 UTC datetime; only pull BIMS products created/modified "
            "since then (incremental). Default: full pull (all manual imports)."
        ),
    )

    subparsers.add_parser(
        "status",
        help="Reconciliation report: BIMS-eligible catalog vs. what's actually in Shopify.",
    )

    dedupe_parser = subparsers.add_parser(
        "dedupe",
        help="Find (and optionally delete) duplicate products created by non-idempotent imports.",
    )
    dedupe_parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the detected duplicates (default: dry run, prints the report only).",
    )
    dedupe_parser.add_argument(
        "--force",
        action="store_true",
        help="Override the safety guard that refuses to delete more than "
        f"{DEDUPE_MAX_DELETE_RATIO:.0%} of the catalog in one run.",
    )

    fix_tracking_parser = subparsers.add_parser(
        "fix-tracking",
        help=(
            "Repair legacy variants created with inventory tracking disabled: enable "
            "tracking and activate them at the tenant location."
        ),
    )
    fix_tracking_parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the repair (default: dry run, reports counts only).",
    )

    cleanup_parser = subparsers.add_parser(
        "cleanup-no-stock",
        help="Draft or delete Shopify products with no stock in BIMS.",
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the cleanup (default: dry run, prints the report only).",
    )
    cleanup_parser.add_argument(
        "--mode",
        choices=["draft", "delete"],
        default="draft",
        help="What to do with zero-stock products: set to DRAFT (default) or delete them.",
    )
    cleanup_parser.add_argument(
        "--force",
        action="store_true",
        help="Override the safety guard that refuses to touch more than "
        f"{CLEANUP_MAX_TARGET_RATIO:.0%} of the catalog in one run.",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    summary = asyncio.run(_run(args))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
