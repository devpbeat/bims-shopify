"""Health check and build-version router."""
from __future__ import annotations

import os

from fastapi import APIRouter

router = APIRouter()

#: Reported when the image was built without the matching build arg (local dev).
UNKNOWN = "unknown"


def _env_or_unknown(name: str) -> str:
    return os.environ.get(name, "").strip() or UNKNOWN


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/version")
async def version() -> dict[str, str]:
    """Public by design: the running commit SHA and build time, nothing else.

    Lets a deploy be verified against the pushed commit. A green CI run only
    means the deploy was triggered, not that the new container is live.
    """
    return {"sha": _env_or_unknown("GIT_SHA"), "built_at": _env_or_unknown("BUILD_TIME")}
