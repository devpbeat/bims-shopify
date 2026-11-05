"""A read-only view of a background ops job."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class OpsJobView:
    id: int
    status: str
    progress_done: int
    progress_total: int | None
    message: str | None
    created_at: datetime | None
    finished_at: datetime | None
    result: dict[str, Any] | None
