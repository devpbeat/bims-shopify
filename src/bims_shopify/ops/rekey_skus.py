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
import json
import os
import re
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

_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*\([^()]*\)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")


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


async def fetch_company_code2_set(bims_client: BIMSClient, company_id: int) -> set[str]:
    """Page through BIMS ``/api/products/index.json`` and collect every ``code2``."""
    code2_values: set[str] = set()
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
        if len(page) < BIMS_INDEX_PAGE_LIMIT:
            return code2_values
        offset += BIMS_INDEX_PAGE_LIMIT


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
    code2_set = await fetch_company_code2_set(bims_client, target_company_id)

    pending: list[dict[str, Any]] = []
    async for variant in shopify_client.iter_all_variants():
        result.total_variants += 1
        sku = (variant.get("sku") or "").strip()
        product = variant.get("product") or {}
        entry = {
            "variant_id": variant.get("id"),
            "product_id": product.get("id"),
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


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.standalone:
        tenant = _build_standalone_tenant(args)
    else:
        if not args.tenant_slug:
            raise SystemExit("tenant_slug is required unless --standalone is used")
        tenant = await _load_tenant_from_db(args.tenant_slug)

    shopify_client = ShopifyClient(tenant)
    bims_client = BIMSClient(tenant)
    try:
        result = await scan(shopify_client, bims_client, target_company_id=args.company)
        summary = result.to_summary()
        if args.apply and result.planned_rewrites:
            apply_result = await apply_rewrites(shopify_client, result.planned_rewrites)
            summary["apply"] = apply_result.to_summary()
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
