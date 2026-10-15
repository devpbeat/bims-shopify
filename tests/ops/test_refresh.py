"""Tests for the composite ``refresh`` ops command (import -> sync -> publish)."""
from __future__ import annotations

import asyncio

import pytest

from bims_shopify.ops import catalog, refresh_job, sync_job
from bims_shopify.ops.refresh_job import RefreshFailed, run_refresh

IMPORT_REPORT = {"products": 3, "variants": 5, "apply": {"created": 3, "failed": 1, "failed_details": [{}]}}
SYNC_REPORT = {"status": "ok", "synced_deltas": 5}
PUBLISH_REPORT = {"total_products": 3, "apply": {"processed": 2, "failed": [{"product_id": "x"}]}}


@pytest.fixture
def calls(monkeypatch):
    recorded: list[tuple[str, dict]] = []

    def fake(name, report, ticks=None):
        async def _runner(tenant, options, progress=None):
            recorded.append((name, dict(options)))
            if progress is not None:
                await progress(7, 10, "working")
            return report

        return _runner

    monkeypatch.setattr(catalog, "run_import", fake("import", IMPORT_REPORT))
    monkeypatch.setattr(sync_job, "run_sync", fake("sync", SYNC_REPORT))
    monkeypatch.setattr(catalog, "run_publish", fake("publish", PUBLISH_REPORT))
    return recorded


async def test_phases_run_in_order_and_report_nests_verbatim(tenant, calls):
    report = await run_refresh(tenant, {"apply": True})

    assert [name for name, _ in calls] == ["import", "sync", "publish"]
    assert report["phases"] == {
        "import": IMPORT_REPORT,
        "sync": SYNC_REPORT,
        "publish": PUBLISH_REPORT,
    }
    assert report["phases"]["import"] is IMPORT_REPORT
    assert report["summary"] == {
        "dry_run": False,
        "products_created": 3,
        "quantities_synced": 5,
        "products_published": 2,
        "failures": 2,
    }
    assert "failed_phase" not in report


async def test_dry_run_is_default_and_reaches_every_phase(tenant, calls):
    report = await run_refresh(tenant, {})

    opts = dict(calls)
    assert opts["import"]["apply"] is False
    assert opts["publish"]["apply"] is False
    assert opts["sync"]["dry_run"] is True
    assert report["summary"]["dry_run"] is True


async def test_apply_true_means_sync_is_not_dry_run(tenant, calls):
    await run_refresh(tenant, {"apply": True})

    opts = dict(calls)
    assert opts["sync"]["dry_run"] is False
    assert opts["import"]["apply"] is True
    assert opts["publish"]["apply"] is True


async def test_options_reach_only_their_phase(tenant, calls):
    await run_refresh(
        tenant, {"apply": True, "only_with_stock": True, "require_images": False, "limit": 25}
    )

    opts = dict(calls)
    assert opts["import"] == {"apply": True, "only_with_stock": True}
    assert opts["publish"] == {"apply": True, "require_images": False, "limit": 25}
    assert opts["sync"] == {"dry_run": False}


async def test_defaults_for_phase_options(tenant, calls):
    await run_refresh(tenant, {"apply": True})

    opts = dict(calls)
    assert opts["import"]["only_with_stock"] is False
    assert opts["publish"]["require_images"] is True
    assert opts["publish"]["limit"] is None


async def test_failing_middle_phase_stops_chain_and_reports_partial(tenant, monkeypatch, calls):
    async def boom(tenant, options, progress=None):
        raise RuntimeError("bims down")

    monkeypatch.setattr(sync_job, "run_sync", boom)

    with pytest.raises(RefreshFailed) as excinfo:
        await run_refresh(tenant, {"apply": True})

    assert [name for name, _ in calls] == ["import"]
    report = excinfo.value.report
    assert report["failed_phase"] == "sync"
    assert report["error"] == "RuntimeError: bims down"
    assert report["phases"] == {"import": IMPORT_REPORT}
    assert "summary" in report


async def test_system_exit_in_a_phase_is_treated_as_failure(tenant, monkeypatch, calls):
    async def bail(tenant, options, progress=None):
        raise SystemExit("safety guard")

    monkeypatch.setattr(catalog, "run_import", bail)

    with pytest.raises(RefreshFailed) as excinfo:
        await run_refresh(tenant, {"apply": True})

    assert calls == []
    assert excinfo.value.report["failed_phase"] == "import"
    assert excinfo.value.report["phases"] == {}


async def test_sync_needing_confirmation_stops_before_publish(tenant, monkeypatch, calls):
    async def needs(tenant, options, progress=None):
        return {"status": "needs_confirmation", "reason": "too many zeros"}

    monkeypatch.setattr(sync_job, "run_sync", needs)

    with pytest.raises(RefreshFailed) as excinfo:
        await run_refresh(tenant, {"apply": True})

    assert [name for name, _ in calls] == ["import"]
    report = excinfo.value.report
    assert report["failed_phase"] == "sync"
    assert "needs_confirmation" in report["error"]
    assert report["phases"]["sync"]["status"] == "needs_confirmation"


@pytest.mark.parametrize("status", ["pull_rejected", "push_declined"])
async def test_a_sync_that_pulled_or_pushed_nothing_stops_before_publish(
    tenant, monkeypatch, calls, status
):
    """publish gates on stock: if the sync could not refresh it (the pull was
    rejected, or the push declined a stale snapshot), publishing acts on old stock."""

    async def nothing_pushed(tenant, options, progress=None):
        return {"status": status, "reason": "too_few_rows"}

    monkeypatch.setattr(sync_job, "run_sync", nothing_pushed)

    with pytest.raises(RefreshFailed) as excinfo:
        await run_refresh(tenant, {"apply": True})

    assert [name for name, _ in calls] == ["import"]
    assert excinfo.value.report["failed_phase"] == "sync"
    assert status in excinfo.value.report["error"]


async def test_progress_messages_carry_phase_prefix(tenant, calls):
    seen: list[tuple[int, int | None, str]] = []

    async def progress(done, total, message):
        seen.append((done, total, message))

    await run_refresh(tenant, {"apply": True}, progress)

    assert seen == [
        (7, 10, "[1/3 import] working"),
        (7, 10, "[2/3 sync] working"),
        (7, 10, "[3/3 publish] working"),
    ]


async def test_sync_phase_holds_the_shared_sync_lock_only_while_running(tenant, monkeypatch, calls):
    lock = asyncio.Lock()
    held: dict[str, bool] = {}

    def watcher(name, report):
        async def _runner(tenant, options, progress=None):
            held[name] = lock.locked()
            return report

        return _runner

    monkeypatch.setattr(catalog, "run_import", watcher("import", IMPORT_REPORT))
    monkeypatch.setattr(sync_job, "run_sync", watcher("sync", SYNC_REPORT))
    monkeypatch.setattr(catalog, "run_publish", watcher("publish", PUBLISH_REPORT))

    await run_refresh(tenant, {"apply": True}, sync_lock=lock)

    assert held == {"import": False, "sync": True, "publish": False}
    assert not lock.locked()


def test_module_is_importable_from_package():
    assert refresh_job.run_refresh is run_refresh


def test_cli_parser_exposes_refresh_with_phase_flags():
    args = catalog._build_arg_parser().parse_args(
        ["acme", "refresh", "--apply", "--only-with-stock", "--limit", "9", "--no-require-images"]
    )
    assert (args.command, args.apply, args.only_with_stock, args.limit, args.require_images) == (
        "refresh", True, True, 9, False,
    )
    defaults = catalog._build_arg_parser().parse_args(["acme", "refresh"])
    assert (defaults.apply, defaults.only_with_stock, defaults.limit, defaults.require_images) == (
        False, False, None, True,
    )
