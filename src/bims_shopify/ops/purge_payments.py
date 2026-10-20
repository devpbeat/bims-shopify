"""Purge one tenant's payment records (``payment_intents`` + payment events).

Usage (operator CLI only; there is no HTTP route and no schedule)::

    # Dry run (default): prints what WOULD be deleted, deletes nothing.
    python -m bims_shopify.ops.purge_payments <tenant_slug> [--provider bancard_vpos] \\
        [--created-after YYYY-MM-DD] [--created-before YYYY-MM-DD]

    # Real deletion: repeat the tenant slug to confirm.
    python -m bims_shopify.ops.purge_payments <tenant_slug> --confirm-tenant <tenant_slug>

Purged for the tenant and provider: ``payment_intents`` rows and
``processed_events`` rows whose source is ``payment:<provider>`` or
``payment_link:<provider>``. Audit logs are kept and a ``payments.purged``
entry with the real counts is appended. The Shopify orders are never touched.

``payment_intents`` does not record the vPOS environment it was created under,
so there is no ``--environment`` filter; use the date bounds instead. A tenant
whose vPOS environment is configured as ``production`` also needs
``--allow-production``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.database import create_engine_and_sessionmaker
from bims_shopify.adapters.persistence.payment_purge_repository import (
    SqlAlchemyPaymentPurgeRepository,
)
from bims_shopify.application.payment_purge import (
    PurgeRefusedError,
    PurgeResult,
    PurgeTenantPayments,
    TenantNotFoundError,
)
from bims_shopify.config import get_settings


async def run_purge(
    session_factory,
    *,
    tenant_slug: str,
    provider: str = "bancard_vpos",
    created_before: datetime | None = None,
    created_after: datetime | None = None,
    confirm_tenant: str | None = None,
    allow_production: bool = False,
    out: Callable[[str], None] = print,
) -> PurgeResult:
    async with session_factory() as session:
        use_case = PurgeTenantPayments(
            SqlAlchemyPaymentPurgeRepository(session), SqlAlchemyAuditLogger(session)
        )
        result = await use_case.run(
            tenant_slug=tenant_slug,
            provider=provider,
            created_before=created_before,
            created_after=created_after,
            confirm_tenant=confirm_tenant,
            allow_production=allow_production,
        )
    _report(result, out)
    return result


def _report(result: PurgeResult, out: Callable[[str], None]) -> None:
    plan, tenant = result.plan, result.tenant
    out(f"Tenant: {tenant.slug} | provider: {result.scope.provider} | vPOS environment: {tenant.environment}")
    if tenant.environment == "production":
        out("WARNING: this tenant is configured for PRODUCTION.")
    out(f"payment_intents: {plan.intents} (by status: {json.dumps(plan.by_status, sort_keys=True)})")
    out(f"processed_events (payment sources): {plan.events}")
    out(f"oldest: {plan.oldest} | newest: {plan.newest}")
    for row in plan.sample:
        out(f"  sample: {json.dumps(row, sort_keys=True)}")
    if result.dry_run:
        out(f"DRY RUN: nothing was deleted. Re-run with --confirm-tenant {tenant.slug} to delete.")
    else:
        out(f"DELETED: {result.intents} payment_intents, {result.events} processed_events.")


def _parse_date(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Purge one tenant's payment records. Dry run unless --confirm-tenant is given."
    )
    parser.add_argument("tenant_slug")
    parser.add_argument("--provider", default="bancard_vpos")
    parser.add_argument("--created-after", type=_parse_date, help="Inclusive UTC date, YYYY-MM-DD.")
    parser.add_argument("--created-before", type=_parse_date, help="Exclusive UTC date, YYYY-MM-DD.")
    parser.add_argument(
        "--confirm-tenant",
        help="Delete for real; must equal the tenant slug. Without it nothing is deleted.",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="Also allow a tenant configured for the vPOS production environment.",
    )
    return parser


async def _main(args: argparse.Namespace) -> PurgeResult:
    engine, session_factory = create_engine_and_sessionmaker(get_settings())
    try:
        return await run_purge(
            session_factory,
            tenant_slug=args.tenant_slug,
            provider=args.provider,
            created_before=args.created_before,
            created_after=args.created_after,
            confirm_tenant=args.confirm_tenant,
            allow_production=args.allow_production,
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    try:
        asyncio.run(_main(args))
    except TenantNotFoundError as exc:
        raise SystemExit(f"tenant '{exc}' not found") from exc
    except PurgeRefusedError as exc:
        raise SystemExit(f"refused: {exc}. Nothing was deleted.") from exc


if __name__ == "__main__":
    main()
