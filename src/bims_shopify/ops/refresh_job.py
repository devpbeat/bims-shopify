"""Composite ops command: ``refresh`` = import -> sync -> publish, as one job.

Getting a BIMS product live in the store takes three existing ops jobs run in
order: ``import`` (creates DRAFT products, no stock), ``sync`` (pushes BIMS
stock) and ``publish`` (ACTIVE + Online Store, gated on stock). ``refresh``
chains the three existing runners sequentially -- stock must land before
publish evaluates its stock gate -- and returns one combined report with each
phase's own report nested verbatim under ``phases``.

Failure policy: if a phase raises (or ``sync`` reports ``needs_confirmation``,
meaning nothing was pushed), the chain stops and :class:`RefreshFailed` is
raised carrying the partial report (``failed_phase``, ``error`` and the phases
that did complete). Raising -- instead of returning -- makes the job end as
``failed``, so a partial result can never look like a success.

Dry-run: ``apply`` (default False) maps to ``apply`` for import/publish and to
``dry_run = not apply`` for sync, whose option is inverted.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from bims_shopify.domain.tenant import Tenant
from bims_shopify.ops import catalog, sync_job
from bims_shopify.ops.catalog import ProgressFn, _emit_progress

PHASES = ("import", "sync", "publish")


class RefreshFailed(Exception):
    """A refresh phase failed; ``report`` holds everything completed so far."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__(f"refresh failed in phase '{report['failed_phase']}': {report['error']}")


def _summarize(phases: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    imported = phases.get("import") or {}
    synced = phases.get("sync") or {}
    published = phases.get("publish") or {}
    import_apply = imported.get("apply") or {}
    publish_apply = published.get("apply") or {}
    return {
        "dry_run": dry_run,
        "products_created": import_apply.get("created", 0),
        "quantities_synced": synced.get("synced_deltas", 0),
        "products_published": publish_apply.get("processed", 0),
        "failures": int(import_apply.get("failed", 0)) + len(publish_apply.get("failed") or []),
    }


def _phase_progress(progress: ProgressFn | None, index: int, name: str) -> ProgressFn | None:
    if progress is None:
        return None

    async def _forward(done: int, total: int | None, message: str) -> None:
        await _emit_progress(progress, done, total, f"[{index}/{len(PHASES)} {name}] {message}")

    return _forward


async def run_refresh(
    tenant: Tenant,
    options: dict[str, Any],
    progress: ProgressFn | None = None,
    *,
    sync_lock: asyncio.Lock | None = None,
) -> dict[str, Any]:
    """Run import -> sync -> publish; see the module docstring.

    ``sync_lock`` is the tenant's shared sync lock (the one the scheduler tick
    and manual HTTP sync use). It is held only around the sync phase, never
    around the (long) import and publish phases.
    """
    apply = bool(options.get("apply", False))
    runners: dict[str, tuple[Callable[..., Awaitable[dict[str, Any]]], dict[str, Any]]] = {
        "import": (
            catalog.run_import,
            {"apply": apply, "only_with_stock": bool(options.get("only_with_stock", False))},
        ),
        # sync_job.run_sync takes the inverted flag: dry_run, not apply.
        "sync": (sync_job.run_sync, {"dry_run": not apply}),
        "publish": (
            catalog.run_publish,
            {
                "apply": apply,
                "require_images": bool(options.get("require_images", True)),
                "limit": options.get("limit"),
            },
        ),
    }

    phases: dict[str, Any] = {}

    def fail(name: str, error: str) -> RefreshFailed:
        return RefreshFailed(
            {
                "phases": phases,
                "failed_phase": name,
                "error": error,
                "summary": _summarize(phases, dry_run=not apply),
            }
        )

    for index, name in enumerate(PHASES, start=1):
        runner, phase_options = runners[name]
        phase_progress = _phase_progress(progress, index, name)
        try:
            if name == "sync" and sync_lock is not None:
                async with sync_lock:
                    result = await runner(tenant, phase_options, phase_progress)
            else:
                result = await runner(tenant, phase_options, phase_progress)
        except (Exception, SystemExit) as exc:
            raise fail(name, f"{type(exc).__name__}: {exc}") from exc
        phases[name] = result
        if name == "sync" and result.get("status") == "needs_confirmation":
            # Nothing was pushed; publishing now would act on stale stock.
            raise fail(name, f"needs_confirmation: {result.get('reason')}")

    return {"phases": phases, "summary": _summarize(phases, dry_run=not apply)}
