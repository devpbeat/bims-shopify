"""SQLAlchemy-backed AuditLogger: fire-and-forget writes to ``audit_logs``.

An audit failure (DB down, session in a bad state, whatever) must never
break the business operation it describes, so every failure here is caught
and logged as a warning instead of propagated. Callers are expected to
`await logger.log(...)` inline (not detach it into a background task): the
guarantee this class provides is "never raises", not "never blocks".
"""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.logging import get_logger

from .models import AuditLogModel

logger = get_logger(__name__)

MAX_AUDIT_LIMIT = 200
DEFAULT_AUDIT_LIMIT = 50


class SqlAlchemyAuditLogger:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        *,
        actor: str,
        action: str,
        entity: str,
        tenant_id: int | None = None,
        entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._session.add(
                AuditLogModel(
                    tenant_id=tenant_id,
                    actor=actor,
                    action=action,
                    entity=entity,
                    entity_id=str(entity_id) if entity_id is not None else None,
                    payload=payload,
                )
            )
            await self._session.commit()
        except Exception as exc:  # audit must never break the caller's operation
            logger.warning(
                "audit_log_write_failed",
                actor=actor,
                action=action,
                entity=entity,
                tenant_id=tenant_id,
                error=str(exc),
            )
            with contextlib.suppress(Exception):  # session may already be unusable
                await self._session.rollback()

    async def list_entries(
        self,
        *,
        tenant_id: int | None = None,
        limit: int = DEFAULT_AUDIT_LIMIT,
        before: datetime | None = None,
    ) -> list[AuditLogModel]:
        """List entries newest-first, optionally scoped to a tenant and cursor-paginated.

        ``before`` is a ``created_at`` cursor (exclusive): pass the
        ``created_at`` of the last row from the previous page to get the
        next one. ``limit`` is clamped to ``[1, MAX_AUDIT_LIMIT]``.
        """
        clamped_limit = max(1, min(limit, MAX_AUDIT_LIMIT))
        stmt = select(AuditLogModel).order_by(
            AuditLogModel.created_at.desc(), AuditLogModel.id.desc()
        )
        if tenant_id is not None:
            stmt = stmt.where(AuditLogModel.tenant_id == tenant_id)
        if before is not None:
            stmt = stmt.where(AuditLogModel.created_at < before)
        stmt = stmt.limit(clamped_limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_latest(
        self, *, action: str, tenant_id: int | None = None
    ) -> AuditLogModel | None:
        """Return the most recent entry for ``action``, optionally scoped to a tenant."""
        stmt = select(AuditLogModel).where(AuditLogModel.action == action)
        if tenant_id is not None:
            stmt = stmt.where(AuditLogModel.tenant_id == tenant_id)
        stmt = stmt.order_by(AuditLogModel.created_at.desc(), AuditLogModel.id.desc()).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
