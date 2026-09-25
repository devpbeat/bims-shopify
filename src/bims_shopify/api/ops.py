"""Admin API for running long catalog/rekey ops commands as background jobs.

Replaces SSH+CLI access to `python -m bims_shopify.ops.catalog` /
`ops.rekey_skus`: POST creates a job row and launches it in the background
via `JobRunner`; GET endpoints let an admin poll progress/result without a
terminal.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.models import OpsJobModel
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.ops.job_runner import JobConflictError, JobRunner

from .deps import get_db_session, get_tenant_repository, require_admin

router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(require_admin)])

_VALID_COMMANDS = {
    "status",
    "wipe",
    "import",
    "dedupe",
    "rekey",
    "fix_tracking",
    "cleanup_no_stock",
    "sync",
}


class RunJobRequest(BaseModel):
    command: Literal[
        "status",
        "wipe",
        "import",
        "dedupe",
        "rekey",
        "fix_tracking",
        "cleanup_no_stock",
        "sync",
    ]
    options: dict[str, Any] = Field(default_factory=dict)


def _validate_options(command: str, options: dict[str, Any]) -> dict[str, Any]:
    """Whitelist + sanity-check options per command; never trust the raw body."""
    if command == "wipe":
        if options.get("confirm") is not True:
            raise HTTPException(status_code=422, detail="wipe requires options.confirm == true")
        return {"confirm": True}

    if command == "import":
        limit = options.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise HTTPException(status_code=422, detail="options.limit must be a non-negative integer")
        return {
            "apply": bool(options.get("apply", False)),
            "publish": bool(options.get("publish", False)),
            "only_with_stock": bool(options.get("only_with_stock", False)),
            "limit": limit,
        }

    if command == "dedupe":
        return {
            "apply": bool(options.get("apply", False)),
            "force": bool(options.get("force", False)),
        }

    if command == "rekey":
        return {
            "apply": bool(options.get("apply", False)),
            "auto_resolve": bool(options.get("auto_resolve", False)),
        }

    if command == "fix_tracking":
        return {"apply": bool(options.get("apply", False))}

    if command == "sync":
        result: dict[str, Any] = {}
        for key in ("dry_run", "full", "force"):
            value = options.get(key, False)
            if not isinstance(value, bool):
                raise HTTPException(status_code=422, detail=f"options.{key} must be a boolean")
            result[key] = value
        return result

    if command == "cleanup_no_stock":
        mode = options.get("mode", "draft")
        if mode not in ("draft", "delete"):
            raise HTTPException(status_code=422, detail="options.mode must be 'draft' or 'delete'")
        return {
            "apply": bool(options.get("apply", False)),
            "mode": mode,
            "force": bool(options.get("force", False)),
        }

    # status
    return {}


def _get_job_runner(request: Request) -> JobRunner:
    runner = getattr(request.app.state, "job_runner", None)
    if runner is None:
        raise RuntimeError("job_runner is not configured on app.state")
    return runner


def _job_to_dict(job: OpsJobModel, *, include_result: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": job.id,
        "tenant_id": job.tenant_id,
        "command": job.command,
        "status": job.status,
        "progress_done": job.progress_done,
        "progress_total": job.progress_total,
        "message": job.message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
    }
    if include_result:
        payload["options"] = job.options
        payload["result"] = job.result
    return payload


@router.post("/{tenant_slug}/run")
async def run_job(
    tenant_slug: str,
    body: RunJobRequest,
    request: Request,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
) -> dict[str, Any]:
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    options = _validate_options(body.command, body.options)
    runner = _get_job_runner(request)
    try:
        job = await runner.start_job(tenant.id, body.command, options)
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"job_id": job.id, "status": job.status}


@router.get("/{tenant_slug}/jobs")
async def list_jobs(
    tenant_slug: str,
    limit: int = 20,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    capped_limit = max(1, min(limit, 100))
    result = await session.execute(
        select(OpsJobModel)
        .where(OpsJobModel.tenant_id == tenant.id)
        .order_by(OpsJobModel.created_at.desc())
        .limit(capped_limit)
    )
    jobs = result.scalars().all()
    return [_job_to_dict(job, include_result=False) for job in jobs]


@router.get("/{tenant_slug}/jobs/{job_id}")
async def get_job(
    tenant_slug: str,
    job_id: int,
    repo: SqlAlchemyTenantRepository = Depends(get_tenant_repository),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    tenant = await repo.get_by_slug(tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    result = await session.execute(
        select(OpsJobModel).where(OpsJobModel.id == job_id, OpsJobModel.tenant_id == tenant.id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")

    return _job_to_dict(job, include_result=True)
