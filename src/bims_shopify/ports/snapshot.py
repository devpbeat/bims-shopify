"""Port for the persisted BIMS catalog snapshot."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from bims_shopify.domain.snapshot import CatalogPull, CatalogSnapshot, SnapshotMeta


class SnapshotStore(Protocol):
    async def stage(self, tenant_id: int, pull: CatalogPull, started_at: datetime) -> SnapshotMeta:
        """Write ``pull`` as an invisible generation (see ``promote``)."""
        ...

    async def promote(
        self,
        snapshot_id: int,
        finished_at: datetime,
        stats: dict[str, Any] | None = None,
        extra_timings: dict[str, Any] | None = None,
    ) -> SnapshotMeta:
        """Atomically make a staged generation the current snapshot."""
        ...

    async def set_stats(self, snapshot_id: int, stats: dict[str, Any]) -> None: ...

    async def record_rejected(
        self,
        tenant_id: int,
        pull: CatalogPull,
        *,
        started_at: datetime,
        finished_at: datetime,
        rejection: dict[str, Any],
    ) -> SnapshotMeta:
        """Record a refused pull (metadata only; the current snapshot is untouched)."""
        ...

    async def get_current_meta(self, tenant_id: int) -> SnapshotMeta | None: ...

    async def load_current(self, tenant_id: int) -> CatalogSnapshot | None: ...
