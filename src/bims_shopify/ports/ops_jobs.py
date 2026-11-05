"""Read side of the background ops jobs, as the portal needs it."""
from __future__ import annotations

from typing import Protocol

from bims_shopify.domain.ops_job import OpsJobView


class OpsJobReader(Protocol):
    async def latest(self, tenant_id: int, command: str) -> OpsJobView | None:
        """The tenant's most recent job of ``command`` in any state, without its result."""
        ...

    async def latest_succeeded(self, tenant_id: int, command: str) -> OpsJobView | None:
        """The tenant's most recent SUCCEEDED job of ``command``, with its result."""
        ...
