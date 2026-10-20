"""Operator-only purge of one tenant's payment records (no HTTP route, no schedule)."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

from bims_shopify.adapters.persistence.audit_repository import SqlAlchemyAuditLogger
from bims_shopify.adapters.persistence.payment_purge_repository import (
    SqlAlchemyPaymentPurgeRepository,
)
from bims_shopify.application.payment_purge import (
    PurgeResult,
    PurgeTenantPayments,
)


async def run_purge(
    session_factory,
    *,
    tenant_slug: str,
    provider: str = "bancard_vpos",
    created_before: datetime | None = None,
    created_after: datetime | None = None,
    confirm_tenant: str | None = None,
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
        )
    _report(result, out)
    return result


def _report(result: PurgeResult, out: Callable[[str], None]) -> None:
    plan, tenant = result.plan, result.tenant
    out(f"Tenant: {tenant.slug} | provider: {result.scope.provider} | vPOS environment: {tenant.environment}")
    out(f"payment_intents: {plan.intents} (by status: {json.dumps(plan.by_status, sort_keys=True)})")
    out(f"processed_events (payment sources): {plan.events}")
    out(f"oldest: {plan.oldest} | newest: {plan.newest}")
    for row in plan.sample:
        out(f"  sample: {json.dumps(row, sort_keys=True)}")
    if result.dry_run:
        out(f"DRY RUN: nothing was deleted. Re-run with --confirm-tenant {tenant.slug} to delete.")
    else:
        out(f"DELETED: {result.intents} payment_intents, {result.events} processed_events.")
