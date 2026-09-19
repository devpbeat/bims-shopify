"""Detect and rewrite stale Shopify variant SKUs against BIMS company-6 ``code2`` values.

Usage::

    python -m bims_shopify.ops.rekey_skus <tenant_slug> [--apply]
    python -m bims_shopify.ops.rekey_skus --standalone --shop <domain> \\
        --shopify-token-env <ENV_VAR> --bims-key-env <ENV_VAR> \\
        --company 6 --bims-url https://in.bims.app [--apply]

Every Shopify product variant is inspected once. BIMS company 6 (MS CLUB) is
the source-of-truth SKU catalog: its ``code2`` values are the "correct" SKUs.
A variant's current SKU is checked against that set; if it is missing, the
SKU is treated as a legacy BIMS company-1 ``Product`` id and looked up via
``/api/products/view.json`` to see whether it maps to a company-6 ``code2``.

Concurrency note: the BIMS client (``bims_shopify.adapters.bims.client.BIMSClient``)
is built on ``httpx.AsyncClient``, same as the Shopify client, so the bounded
BIMS-lookup concurrency required by spec is implemented with an
``asyncio.Semaphore`` rather than a ``ThreadPoolExecutor`` — a thread pool
would only make sense if the underlying client were synchronous.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet

from bims_shopify.adapters.bims.client import BIMSAPIError, BIMSClient
from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.database import create_engine_and_sessionmaker
from bims_shopify.adapters.persistence.models import RekeyReportModel
from bims_shopify.adapters.persistence.tenant_repository import SqlAlchemyTenantRepository
from bims_shopify.adapters.shopify.client import ShopifyClient, ShopifyGraphQLError
from bims_shopify.config import get_settings
from bims_shopify.domain.tenant import Tenant

TARGET_COMPANY_ID = 6
BIMS_INDEX_PAGE_LIMIT = 2000
BIMS_LOOKUP_CONCURRENCY = 5
BIMS_LOOKUP_TIMEOUT_SECONDS = 30.0
NAME_MATCH_RATIO_THRESHOLD = 0.6
_DETAIL_PREVIEW_COUNT = 10
MAX_SCAN_AGE_SECONDS = 24 * 60 * 60

_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*\([^()]*\)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")

#: Shared with ops/catalog.py's progress contract: (done, total-or-None, message).
ProgressFn = Callable[[int, int | None, str], "Awaitable[None] | None"]


async def _emit_progress(progress: ProgressFn | None, done: int, total: int | None, message: str) -> None:
    if progress is None:
        return
    outcome = progress(done, total, message)
    if inspect.isawaitable(outcome):
        await outcome


def normalize_name(name: str) -> str:
    """Normalize a product name for fuzzy comparison.

    Strips a single trailing parenthetical suffix (e.g. size/color
    variants like " (XL)"), casefolds, strips, and collapses internal
    whitespace.
    """
    stripped = _TRAILING_PARENTHETICAL_RE.sub("", name or "")
    stripped = stripped.strip().casefold()
    return _WHITESPACE_RE.sub(" ", stripped)


def names_match(bims_name: str, shopify_title: str) -> bool:
    """True if the two product names are close enough to be the same product."""
    normalized_bims = normalize_name(bims_name)
    normalized_shopify = normalize_name(shopify_title)
    if not normalized_bims or not normalized_shopify:
        return False
    if normalized_bims in normalized_shopify or normalized_shopify in normalized_bims:
        return True
    ratio = difflib.SequenceMatcher(None, normalized_bims, normalized_shopify).ratio()
    return ratio > NAME_MATCH_RATIO_THRESHOLD


#: Exceptions that represent a transient/per-lookup failure against the BIMS
#: API (network blips, protocol resets, timeouts, or a malformed JSON body).
#: A single lookup failing this way must not abort the whole scan.
_TRANSIENT_LOOKUP_EXCEPTIONS: tuple[type[Exception], ...] = (
    BIMSAPIError,
    httpx.HTTPStatusError,
    httpx.TransportError,
    TimeoutError,
    json.JSONDecodeError,
)


@dataclass
class ScanResult:
    total_variants: int = 0
    empty_sku: int = 0
    already_keyed: int = 0
    planned_rewrites: list[dict[str, Any]] = field(default_factory=list)
    name_mismatch: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    duplicate_target: list[dict[str, Any]] = field(default_factory=list)
    #: product_id -> number of variants seen for that product in this scan.
    #: Used by auto-resolve to detect "last remaining variant" before deleting.
    product_variant_counts: dict[str, int] = field(default_factory=dict)
    #: company-6 code2 -> BIMS company-1 Product id, when the index row carried one.
    code2_to_bims_id: dict[str, str] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        return {
            "total_variants": self.total_variants,
            "empty_sku": self.empty_sku,
            "already_keyed": self.already_keyed,
            "planned_rewrites": len(self.planned_rewrites),
            "name_mismatch": len(self.name_mismatch),
            "unresolved": len(self.unresolved),
            "duplicate_target": len(self.duplicate_target),
            "details": {
                "planned_rewrites": self.planned_rewrites[:_DETAIL_PREVIEW_COUNT],
                "name_mismatch": self.name_mismatch[:_DETAIL_PREVIEW_COUNT],
                "unresolved": self.unresolved[:_DETAIL_PREVIEW_COUNT],
                "duplicate_target": self.duplicate_target[:_DETAIL_PREVIEW_COUNT],
            },
        }


@dataclass
class ApplyResult:
    """Outcome of :func:`apply_rewrites`, isolated per product group."""

    succeeded: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "products_succeeded": len(self.succeeded),
            "products_failed": len(self.failed),
            "details": {
                "succeeded": self.succeeded[:_DETAIL_PREVIEW_COUNT],
                "failed": self.failed[:_DETAIL_PREVIEW_COUNT],
            },
        }


@dataclass
class AutoResolveResult:
    """Outcome of :func:`auto_resolve_conflicts`."""

    survivors: list[dict[str, Any]] = field(default_factory=list)
    deleted_variants: list[dict[str, Any]] = field(default_factory=list)
    drafted_products: list[dict[str, Any]] = field(default_factory=list)
    mismatch_rewrites: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "survivors": len(self.survivors),
            "deleted_variants": len(self.deleted_variants),
            "drafted_products": len(self.drafted_products),
            "mismatch_rewrites": len(self.mismatch_rewrites),
            "failures": self.failures,
        }


async def fetch_company6_catalog(
    bims_client: BIMSClient, company_id: int
) -> tuple[set[str], dict[str, str]]:
    """Page through BIMS ``/api/products/index.json`` for the company-6 catalog.

    Returns ``(code2_values, code2_to_bims_id)``: the full set of valid
    ``code2`` SKUs, and a mapping from ``code2`` to the BIMS product ``id``
    that owns it (when the index row carries an id). The id map is used by
    auto-resolve to break duplicate-target ties deterministically.
    """
    code2_values: set[str] = set()
    code2_to_bims_id: dict[str, str] = {}
    offset = 0
    while True:
        body = await bims_client.get(
            "/api/products/index.json",
            params={
                "mode": "simple",
                "company_id": company_id,
                "limit": BIMS_INDEX_PAGE_LIMIT,
                "offset": offset,
            },
        )
        page = body.get("data") or []
        for item in page:
            # mode=simple rows are wrapped as {"Product": {...}}.
            product = item.get("Product") or item
            code2 = product.get("code2")
            if code2 not in (None, ""):
                code2_values.add(str(code2))
                bims_id = product.get("id")
                if bims_id not in (None, ""):
                    code2_to_bims_id[str(code2)] = str(bims_id)
        if len(page) < BIMS_INDEX_PAGE_LIMIT:
            return code2_values, code2_to_bims_id
        offset += BIMS_INDEX_PAGE_LIMIT


async def fetch_company_code2_set(bims_client: BIMSClient, company_id: int) -> set[str]:
    """Backwards-compatible wrapper returning just the ``code2`` set."""
    code2_values, _ = await fetch_company6_catalog(bims_client, company_id)
    return code2_values


async def _lookup_bims_product(
    bims_client: BIMSClient, semaphore: asyncio.Semaphore, sku: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Look up ``sku`` as a legacy BIMS company-1 Product id via /api/products/view.json.

    Returns ``(product, error)``. ``product`` is the product dict, or None if
    it does not exist. ``error`` is a short description if the lookup itself
    failed (transient network issue, timeout, or malformed response) — such a
    failure means the SKU is "unresolved", not "not found", but must never
    propagate out of this coroutine and abort the rest of the scan (this is
    called with ``asyncio.gather`` across hundreds of SKUs at concurrency 5
    against a live network).
    """
    async with semaphore:
        try:
            body = await asyncio.wait_for(
                bims_client.get("/api/products/view.json", params={"id": sku}),
                timeout=BIMS_LOOKUP_TIMEOUT_SECONDS,
            )
        except _TRANSIENT_LOOKUP_EXCEPTIONS as exc:
            return None, f"{type(exc).__name__}: {exc}"
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        # view.json wraps the record as {"Product": {...}}.
        data = data.get("Product") or data
    return data or None, None


async def scan(
    shopify_client: ShopifyClient,
    bims_client: BIMSClient,
    target_company_id: int = TARGET_COMPANY_ID,
) -> ScanResult:
    """Run the full decision-table scan over every Shopify variant."""
    result = ScanResult()
    code2_set, code2_to_bims_id = await fetch_company6_catalog(bims_client, target_company_id)
    result.code2_to_bims_id = code2_to_bims_id

    pending: list[dict[str, Any]] = []
    async for variant in shopify_client.iter_all_variants():
        result.total_variants += 1
        sku = (variant.get("sku") or "").strip()
        product = variant.get("product") or {}
        product_id = product.get("id")
        if product_id is not None:
            result.product_variant_counts[product_id] = (
                result.product_variant_counts.get(product_id, 0) + 1
            )
        entry = {
            "variant_id": variant.get("id"),
            "product_id": product_id,
            "product_title": product.get("title") or "",
            "sku": sku,
        }

        if not sku:
            result.empty_sku += 1
            continue
        if sku in code2_set:
            result.already_keyed += 1
            continue
        pending.append(entry)

    semaphore = asyncio.Semaphore(BIMS_LOOKUP_CONCURRENCY)
    lookups = await asyncio.gather(
        *(_lookup_bims_product(bims_client, semaphore, entry["sku"]) for entry in pending)
    )

    for entry, (bims_product, lookup_error) in zip(pending, lookups, strict=True):
        code2 = (bims_product or {}).get("code2")
        if bims_product is None or code2 in (None, "") or str(code2) not in code2_set:
            unresolved_entry = {
                "variant_id": entry["variant_id"],
                "product_title": entry["product_title"],
                "sku": entry["sku"],
            }
            if lookup_error is not None:
                unresolved_entry["error"] = lookup_error
            result.unresolved.append(unresolved_entry)
            continue

        bims_name = bims_product.get("name") or ""
        detail = {
            "variant_id": entry["variant_id"],
            "product_id": entry["product_id"],
            "product_title": entry["product_title"],
            "old_sku": entry["sku"],
            "new_sku": str(code2),
            "bims_name": bims_name,
        }
        if names_match(bims_name, entry["product_title"]):
            result.planned_rewrites.append(detail)
        else:
            result.name_mismatch.append(detail)

    result.planned_rewrites, duplicates = partition_duplicate_targets(result.planned_rewrites)
    result.duplicate_target.extend(duplicates)

    return result


def partition_duplicate_targets(
    planned_rewrites: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``planned_rewrites`` into (safe, duplicate-target) lists.

    If two or more planned rewrites resolve to the same ``new_sku``, applying
    all of them would either violate Shopify's SKU-uniqueness constraint or
    silently leave the store in an inconsistent state (whichever update lands
    last "wins"). None of the variants sharing a duplicated target are safe to
    apply automatically, so every one of them is moved into the second list
    instead of being rewritten.
    """
    counts: dict[str, int] = {}
    for item in planned_rewrites:
        counts[item["new_sku"]] = counts.get(item["new_sku"], 0) + 1

    safe: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for item in planned_rewrites:
        if counts[item["new_sku"]] > 1:
            duplicates.append(item)
        else:
            safe.append(item)
    return safe, duplicates


async def apply_rewrites(
    shopify_client: ShopifyClient, planned_rewrites: list[dict[str, Any]]
) -> ApplyResult:
    """Execute planned SKU rewrites, grouped into one bulk mutation per product.

    Each product group is applied independently: if Shopify rejects one
    group's mutation (GraphQL error, userErrors, or a transient network
    failure) that failure is recorded and the remaining product groups are
    still processed, instead of aborting the whole apply run and leaving no
    record of which products already succeeded.
    """
    by_product: dict[str, list[dict[str, Any]]] = {}
    for item in planned_rewrites:
        by_product.setdefault(item["product_id"], []).append(item)

    result = ApplyResult()
    for product_id, items in by_product.items():
        variants_input = [{"id": item["variant_id"], "sku": item["new_sku"]} for item in items]
        try:
            await shopify_client.bulk_update_variants(product_id, variants_input)
        except (ShopifyGraphQLError, httpx.TransportError, TimeoutError) as exc:
            result.failed.append(
                {
                    "product_id": product_id,
                    "variant_ids": [item["variant_id"] for item in items],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            result.succeeded.append(
                {
                    "product_id": product_id,
                    "variant_ids": [item["variant_id"] for item in items],
                }
            )
    return result


async def auto_resolve_conflicts(
    shopify_client: ShopifyClient,
    scan_result: ScanResult,
) -> AutoResolveResult:
    """Automatically resolve conflicts in duplicate_target and unresolved entries.

    For duplicate_target groups:
    - Find survivor: variant whose old_sku matches code2_to_bims_id[code2], else lowest variant_id
    - Rewrite survivor's SKU to the code2 value
    - Delete all sibling variants

    For unresolved entries:
    - Delete the variant
    - If deleting the last variant of a product, set product status to DRAFT instead

    For name_mismatch entries:
    - Include them in the SKU rewrite batch
    """
    result = AutoResolveResult()

    # Rewrite batch: survivors from duplicates + all name_mismatches
    rewrite_batch: list[dict[str, Any]] = []

    # Group duplicate_target by new_sku (each group has multiple variants targeting same code2)
    duplicates_by_sku: dict[str, list[dict[str, Any]]] = {}
    for item in scan_result.duplicate_target:
        duplicates_by_sku.setdefault(item["new_sku"], []).append(item)

    # Process each duplicate group
    for code2, variants in duplicates_by_sku.items():
        # Find survivor: prefer one whose old_sku equals the BIMS product id for this code2
        bims_id = scan_result.code2_to_bims_id.get(code2)
        survivor = None
        if bims_id:
            for v in variants:
                if v["old_sku"] == bims_id:
                    survivor = v
                    break
        # If no BIMS id match, use lowest variant_id
        if survivor is None:
            survivor = min(variants, key=lambda v: v["variant_id"])

        result.survivors.append(
            {
                "variant_id": survivor["variant_id"],
                "product_id": survivor["product_id"],
                "old_sku": survivor["old_sku"],
                "new_sku": survivor["new_sku"],
            }
        )
        rewrite_batch.append(
            {
                "variant_id": survivor["variant_id"],
                "product_id": survivor["product_id"],
                "new_sku": survivor["new_sku"],
            }
        )

        # Delete all siblings
        product_id = survivor["product_id"]
        siblings_to_delete = [v["variant_id"] for v in variants if v["variant_id"] != survivor["variant_id"]]
        for variant_id in siblings_to_delete:
            result.deleted_variants.append({"variant_id": variant_id, "product_id": product_id})

        # Delete sibling variants
        try:
            await shopify_client.bulk_delete_variants(product_id, siblings_to_delete)
        except (ShopifyGraphQLError, httpx.TransportError, TimeoutError) as exc:
            for variant_id in siblings_to_delete:
                result.failures.append(
                    {
                        "variant_id": variant_id,
                        "error": f"delete: {type(exc).__name__}: {exc}",
                    }
                )

    # Process unresolved entries: delete or draft
    by_product_unresolved: dict[str, list[dict[str, Any]]] = {}
    for item in scan_result.unresolved:
        product_id = item.get("product_id")
        if product_id:
            by_product_unresolved.setdefault(product_id, []).append(item)

    for product_id, unresolved_variants in by_product_unresolved.items():
        total_variants = scan_result.product_variant_counts.get(product_id, 0)
        variants_to_delete = unresolved_variants

        # Check if deleting all these variants would remove the last variant of the product
        if len(variants_to_delete) >= total_variants:
            # Keep one variant, set product to DRAFT
            variants_to_delete = variants_to_delete[1:]  # Delete all but first

            try:
                await shopify_client.set_product_status(product_id, "DRAFT")
                result.drafted_products.append({"product_id": product_id})
            except (ShopifyGraphQLError, httpx.TransportError, TimeoutError) as exc:
                result.failures.append(
                    {
                        "product_id": product_id,
                        "error": f"draft: {type(exc).__name__}: {exc}",
                    }
                )
        else:
            # Safe to delete all unresolved variants
            pass

        # Delete the variants
        if variants_to_delete:
            try:
                await shopify_client.bulk_delete_variants(
                    product_id, [v["variant_id"] for v in variants_to_delete]
                )
                for variant in variants_to_delete:
                    result.deleted_variants.append(
                        {"variant_id": variant["variant_id"], "product_id": product_id}
                    )
            except (ShopifyGraphQLError, httpx.TransportError, TimeoutError) as exc:
                for variant in variants_to_delete:
                    result.failures.append(
                        {
                            "variant_id": variant["variant_id"],
                            "error": f"delete: {type(exc).__name__}: {exc}",
                        }
                    )

    # Add name_mismatch entries to rewrite batch
    for item in scan_result.name_mismatch:
        result.mismatch_rewrites.append(
            {
                "variant_id": item["variant_id"],
                "product_id": item["product_id"],
                "old_sku": item["old_sku"],
                "new_sku": item["new_sku"],
            }
        )
        rewrite_batch.append(
            {
                "variant_id": item["variant_id"],
                "product_id": item["product_id"],
                "new_sku": item["new_sku"],
            }
        )

    # Apply all rewrites (survivors + name_mismatches)
    if rewrite_batch:
        by_product_rewrite: dict[str, list[dict[str, Any]]] = {}
        for item in rewrite_batch:
            by_product_rewrite.setdefault(item["product_id"], []).append(item)

        for product_id, items in by_product_rewrite.items():
            variants_input = [{"id": item["variant_id"], "sku": item["new_sku"]} for item in items]
            try:
                await shopify_client.bulk_update_variants(product_id, variants_input)
            except (ShopifyGraphQLError, httpx.TransportError, TimeoutError) as exc:
                for item in items:
                    result.failures.append(
                        {
                            "variant_id": item["variant_id"],
                            "error": f"rewrite: {type(exc).__name__}: {exc}",
                        }
                    )

    return result


_HEADER_FONT = Font(bold=True)


def _write_header_row(ws: Worksheet, headers: list[str], column_widths: list[int]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = _HEADER_FONT
    ws.freeze_panes = "A2"
    for index, width in enumerate(column_widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=index).column_letter].width = width


def build_report_workbook(
    result: ScanResult,
    *,
    shop_domain: str,
    tenant_slug: str,
    scanned_at: datetime,
) -> Workbook:
    """Build the full (untruncated) rekey_skus report as an xlsx workbook."""
    workbook = Workbook()

    summary_ws = workbook.active
    summary_ws.title = "Summary"
    _write_header_row(summary_ws, ["Metric", "Value"], [30, 40])
    summary_rows = [
        ("total_variants", result.total_variants),
        ("empty_sku", result.empty_sku),
        ("already_keyed", result.already_keyed),
        ("planned_rewrites", len(result.planned_rewrites)),
        ("name_mismatch", len(result.name_mismatch)),
        ("unresolved", len(result.unresolved)),
        ("duplicate_target", len(result.duplicate_target)),
        ("scanned_at", scanned_at.isoformat()),
        ("shop_domain", shop_domain),
        ("tenant_slug", tenant_slug),
    ]
    for row in summary_rows:
        summary_ws.append(row)

    duplicates_ws = workbook.create_sheet("Duplicates")
    _write_header_row(
        duplicates_ws,
        [
            "product_title",
            "size/bims_name",
            "old_sku",
            "target_code2(new_sku)",
            "variant_id",
            "product_id",
            "KEEP? (YES/NO)",
        ],
        [30, 20, 15, 20, 30, 30, 16],
    )
    for item in sorted(
        result.duplicate_target, key=lambda item: (item["product_title"], item["new_sku"])
    ):
        duplicates_ws.append(
            [
                item["product_title"],
                item["bims_name"],
                item["old_sku"],
                item["new_sku"],
                item["variant_id"],
                item["product_id"],
                None,
            ]
        )

    unresolved_ws = workbook.create_sheet("Unresolved")
    _write_header_row(
        unresolved_ws,
        ["product_title", "sku", "variant_id", "ACTION"],
        [30, 20, 30, 16],
    )
    for item in result.unresolved:
        unresolved_ws.append(
            [item["product_title"], item["sku"], item["variant_id"], None]
        )

    name_mismatch_ws = workbook.create_sheet("Name mismatch")
    _write_header_row(
        name_mismatch_ws,
        [
            "product_title",
            "bims_name",
            "old_sku",
            "proposed_code2",
            "variant_id",
            "APPROVE? (YES/NO)",
        ],
        [30, 30, 15, 20, 30, 18],
    )
    for item in result.name_mismatch:
        name_mismatch_ws.append(
            [
                item["product_title"],
                item["bims_name"],
                item["old_sku"],
                item["new_sku"],
                item["variant_id"],
                None,
            ]
        )

    return workbook


def write_report_xlsx(
    result: ScanResult,
    path: str,
    *,
    shop_domain: str,
    tenant_slug: str,
    scanned_at: datetime,
) -> None:
    workbook = build_report_workbook(
        result, shop_domain=shop_domain, tenant_slug=tenant_slug, scanned_at=scanned_at
    )
    workbook.save(path)


def _full_result_payload(result: ScanResult) -> dict[str, Any]:
    """Full, untruncated scan result — used for DB persistence, not stdout."""
    return {
        "total_variants": result.total_variants,
        "empty_sku": result.empty_sku,
        "already_keyed": result.already_keyed,
        "planned_rewrites": result.planned_rewrites,
        "name_mismatch": result.name_mismatch,
        "unresolved": result.unresolved,
        "duplicate_target": result.duplicate_target,
    }


async def persist_rekey_report(
    tenant_id: int, payload: dict[str, Any], summary: dict[str, Any] | None = None
) -> None:
    """Persist the full scan result for a DB-mode tenant into ``rekey_reports``.

    Opens a short-lived engine/session scoped to this single write, kept
    separate from the tenant-loading session so callers do not need to
    thread a shared session through the whole scan/apply/rescan flow.
    """
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with session_factory() as session:
            session.add(RekeyReportModel(tenant_id=tenant_id, payload=payload))
            await session.commit()

            audit = SqlAlchemyAuditLogger(session)
            compact_summary = {
                "total_variants": payload.get("total_variants"),
                "planned_rewrites": len(payload.get("planned_rewrites") or []),
                "name_mismatch": len(payload.get("name_mismatch") or []),
                "unresolved": len(payload.get("unresolved") or []),
                "duplicate_target": len(payload.get("duplicate_target") or []),
            }
            if summary and "apply" in summary:
                compact_summary["apply"] = summary["apply"]
            await audit.log(
                actor="system",
                action="rekey.scan_completed",
                entity="rekey_report",
                tenant_id=tenant_id,
                payload=compact_summary,
            )
    finally:
        await engine.dispose()


def _build_standalone_tenant(args: argparse.Namespace) -> Tenant:
    missing = [
        flag
        for flag, value in (
            ("--shop", args.shop),
            ("--shopify-token-env", args.shopify_token_env),
            ("--bims-key-env", args.bims_key_env),
            ("--bims-url", args.bims_url),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"--standalone requires: {', '.join(missing)}")

    shopify_token = os.environ.get(args.shopify_token_env, "")
    if not shopify_token:
        raise SystemExit(f"env var {args.shopify_token_env} is not set or empty")
    bims_key = os.environ.get(args.bims_key_env, "")
    if not bims_key:
        raise SystemExit(f"env var {args.bims_key_env} is not set or empty")

    return Tenant(
        id=None,
        slug="standalone",
        bims_base_url=args.bims_url,
        bims_api_key=bims_key,
        shopify_shop_domain=args.shop,
        shopify_access_token=shopify_token,
        shopify_webhook_secret="",
        shopify_location_id="",
        bims_posale_id=0,
        bims_warehouse_id=0,
        bims_company_id=args.company,
        bims_currency_id=0,
        bims_payment_method_id=0,
        default_customer_contact_id=0,
    )


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


async def _run(args: argparse.Namespace, progress: ProgressFn | None = None) -> dict[str, Any]:
    if args.auto_resolve and not args.apply:
        raise SystemExit("--auto-resolve requires --apply")

    if args.standalone:
        tenant = _build_standalone_tenant(args)
    else:
        if not args.tenant_slug:
            raise SystemExit("tenant_slug is required unless --standalone is used")
        tenant = await _load_tenant_from_db(args.tenant_slug)

    return await _run_for_tenant(tenant, args, progress=progress)


async def _run_for_tenant(
    tenant: Tenant, args: argparse.Namespace, progress: ProgressFn | None = None
) -> dict[str, Any]:
    shopify_client = ShopifyClient(tenant)
    bims_client = BIMSClient(tenant)
    try:
        await _emit_progress(progress, 0, None, "scanning Shopify variants against BIMS company-6")
        result = await scan(shopify_client, bims_client, target_company_id=args.company)
        summary = result.to_summary()
        if args.apply:
            if args.auto_resolve:
                # Auto-resolve all conflicts
                await _emit_progress(progress, 1, None, "auto-resolving conflicts")
                auto_resolve_result = await auto_resolve_conflicts(shopify_client, result)
                summary["auto_resolved"] = auto_resolve_result.to_summary()
            elif result.planned_rewrites:
                # Apply only safe rewrites
                await _emit_progress(progress, 1, None, "applying planned rewrites")
                apply_result = await apply_rewrites(shopify_client, result.planned_rewrites)
                summary["apply"] = apply_result.to_summary()

            await _emit_progress(progress, 2, None, "rescanning after apply")
            result = await scan(shopify_client, bims_client, target_company_id=args.company)
            summary["rescan"] = result.to_summary()

        scanned_at = datetime.now(UTC)
        report_summary = _full_result_payload(result)
        if args.report_xlsx:
            write_report_xlsx(
                result,
                args.report_xlsx,
                shop_domain=tenant.shopify_shop_domain,
                tenant_slug=tenant.slug,
                scanned_at=scanned_at,
            )
            summary["report_path"] = args.report_xlsx

        if not args.standalone and tenant.id is not None:
            await persist_rekey_report(tenant.id, report_summary, summary)

        return summary
    finally:
        await shopify_client.aclose()
        await bims_client.aclose()


# --- In-process entry point for the background JobRunner (api/ops.py) ---


async def run_rekey(tenant: Tenant, options: dict[str, Any], progress: ProgressFn | None = None) -> dict[str, Any]:
    args = argparse.Namespace(
        standalone=False,
        tenant_slug=tenant.slug,
        apply=bool(options.get("apply", False)),
        auto_resolve=bool(options.get("auto_resolve", False)),
        company=int(options.get("company", TARGET_COMPANY_ID)),
        report_xlsx=None,
    )
    return await _run_for_tenant(tenant, args, progress=progress)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rekey stale Shopify variant SKUs against BIMS company-6 code2 values."
    )
    parser.add_argument(
        "tenant_slug",
        nargs="?",
        help="Tenant slug to load from the database (ignored when --standalone is set).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute planned rewrites (default: dry run, prints the plan only).",
    )
    parser.add_argument(
        "--auto-resolve",
        action="store_true",
        help="Automatically resolve duplicates, unresolved, and name_mismatch entries (requires --apply).",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Skip DB/tenant lookup and build clients directly from CLI flags.",
    )
    parser.add_argument("--shop", help="Shopify shop domain (standalone mode).")
    parser.add_argument(
        "--shopify-token-env",
        help="Name of the env var holding the Shopify access token (standalone mode).",
    )
    parser.add_argument(
        "--bims-key-env",
        help="Name of the env var holding the BIMS API key (standalone mode).",
    )
    parser.add_argument(
        "--company",
        type=int,
        default=TARGET_COMPANY_ID,
        help="BIMS company id treated as the source-of-truth SKU catalog (default: 6).",
    )
    parser.add_argument("--bims-url", help="BIMS base URL (standalone mode).")
    parser.add_argument(
        "--report-xlsx",
        dest="report_xlsx",
        help="Write a full (untruncated) xlsx report of the scan result to this path.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    summary = asyncio.run(_run(args))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
