"""Port for the audit trail.

An audit-log write must never break the business operation it describes:
callers invoke ``AuditLogger.log`` and move on, trusting the implementation
to swallow and log (rather than raise) any failure. See
``adapters.persistence.audit_repository.SqlAlchemyAuditLogger``.
"""
from __future__ import annotations

from typing import Any, Protocol


class AuditLogger(Protocol):
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
        """Record one audit entry. Must never raise.

        ``payload`` should stay compact (counts/ids) and must never contain
        secrets, tokens, or API keys.
        """
        ...
