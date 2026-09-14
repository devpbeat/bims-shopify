"""Admin read endpoint for the audit trail."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.audit_repository import (
    DEFAULT_AUDIT_LIMIT,
    MAX_AUDIT_LIMIT,
    SqlAlchemyAuditLogger,
)
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.datetime_utils import ensure_aware_utc

from .deps import get_db_session, get_tenant_repository, require_admin

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(require_admin)])


@router.get("")
async def list_audit_log(
    tenant: str | None = None,
    limit: int = DEFAULT_AUDIT_LIMIT,
    before: datetime | None = None,
    session: AsyncSession = Depends(get_db_session),
    tenant_repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
):
    """List audit entries, newest-first, optionally scoped to a tenant slug.

    ``before`` is a `created_at` cursor for pagination: pass the last row's
    `created_at` from the previous page to fetch the next one.
    """
    clamped_limit = max(1, min(limit, MAX_AUDIT_LIMIT))

    tenant_id: int | None = None
    if tenant is not None:
        tenant_obj = await tenant_repo.get_by_slug(tenant)
        if tenant_obj is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        tenant_id = tenant_obj.id

    audit_repo = SqlAlchemyAuditLogger(session)
    entries = await audit_repo.list_entries(
        tenant_id=tenant_id, limit=clamped_limit, before=before
    )
    return {
        "entries": [
            {
                "id": entry.id,
                "tenant_id": entry.tenant_id,
                "actor": entry.actor,
                "action": entry.action,
                "entity": entry.entity,
                "entity_id": entry.entity_id,
                "payload": entry.payload,
                "created_at": ensure_aware_utc(entry.created_at).isoformat(),
            }
            for entry in entries
        ]
    }
