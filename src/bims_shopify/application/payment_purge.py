"""Purge one tenant's payment records (operator CLI only, dry run by default).

Destroys money records, so the guard rails live here and not in the CLI shell:
the default is a dry run, deletion needs the tenant slug typed back exactly,
and a tenant currently configured for the vPOS production environment needs an
extra explicit override. ``payment_intents`` does not record the vPOS
environment it was created under, so there is deliberately no environment
filter; ``created_before`` / ``created_after`` bound the purge instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from bims_shopify.application.payments import AuditLogPort


class TenantNotFoundError(Exception):
    """No tenant has the requested slug."""


class PurgeRefusedError(Exception):
    """A safety guard blocked the deletion; nothing was deleted."""


@dataclass(frozen=True)
class PurgeTenant:
    id: int
    slug: str
    environment: str | None


@dataclass(frozen=True)
class PurgeScope:
    tenant_id: int
    provider: str
    created_before: datetime | None = None
    created_after: datetime | None = None

    @property
    def event_sources(self) -> tuple[str, str]:
        return (f"payment:{self.provider}", f"payment_link:{self.provider}")


@dataclass(frozen=True)
class PurgePlan:
    intents: int
    events: int
    by_status: dict[str, int] = field(default_factory=dict)
    sample: list[dict[str, Any]] = field(default_factory=list)
    oldest: str | None = None
    newest: str | None = None


@dataclass(frozen=True)
class PurgeResult:
    dry_run: bool
    tenant: PurgeTenant
    scope: PurgeScope
    plan: PurgePlan
    intents: int
    events: int


class PaymentPurgePort(Protocol):
    async def find_tenant(self, slug: str) -> PurgeTenant | None: ...

    async def plan(self, scope: PurgeScope) -> PurgePlan: ...

    async def delete(self, scope: PurgeScope) -> tuple[int, int]:
        """Delete atomically; return the (intents, events) actually removed."""
        ...


class PurgeTenantPayments:
    def __init__(self, repo: PaymentPurgePort, audit: AuditLogPort) -> None:
        self._repo = repo
        self._audit = audit

    async def run(
        self,
        *,
        tenant_slug: str,
        provider: str,
        created_before: datetime | None = None,
        created_after: datetime | None = None,
        confirm_tenant: str | None = None,
        allow_production: bool = False,
    ) -> PurgeResult:
        tenant = await self._repo.find_tenant(tenant_slug)
        if tenant is None:
            raise TenantNotFoundError(tenant_slug)
        scope = PurgeScope(tenant.id, provider, created_before, created_after)
        plan = await self._repo.plan(scope)
        if confirm_tenant is None:
            return PurgeResult(True, tenant, scope, plan, plan.intents, plan.events)
        if confirm_tenant != tenant.slug:
            raise PurgeRefusedError("confirmation does not match the tenant slug")
        if tenant.environment == "production" and not allow_production:
            raise PurgeRefusedError("tenant is configured for production; override required")
        intents, events = await self._repo.delete(scope)
        await self._audit.log(
            actor="cli",
            action="payments.purged",
            entity="payment_intent",
            tenant_id=tenant.id,
            entity_id=tenant.slug,
            payload={
                "provider": provider,
                "intents_deleted": intents,
                "events_deleted": events,
                "created_before": created_before.isoformat() if created_before else None,
                "created_after": created_after.isoformat() if created_after else None,
                "environment": tenant.environment,
            },
        )
        return PurgeResult(False, tenant, scope, plan, intents, events)
