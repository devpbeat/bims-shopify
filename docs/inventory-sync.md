# Scheduled inventory sync

How the scheduled BIMS -> Shopify stock sync decides what to compare, how often
it runs, and how the system stops an operator from scheduling it too tightly.

## Full vs incremental

Every sync pulls products from BIMS (`GET /api/products/index.json`), diffs the
stock against what was last pushed, and pushes the changes to Shopify.

- **Full**: ask BIMS for the whole catalog, every run. Nothing is filtered out.
- **Incremental**: ask BIMS only for products modified since the stored
  watermark (`last_update=<last_run_at - 5 minutes>`).

The mode is a per-tenant setting, `sync_mode` (`full` | `incremental`), applied
to the **scheduled** sync. Manual runs (`POST /sync/{slug}/run`) keep their own
`full` / `force` query parameters.

### Why full is the default

BIMS's `last_update` is unreliable. Measured in production: eight consecutive
incremental runs missed a product that had demonstrably changed, and complete
catalog pulls returned 33,705 vs 11,355 rows hours apart. Incremental runs
reported `{"status":"ok","matched":0,"updated":0}` all day while thousands of
products showed zero stock in the storefront despite real stock in BIMS.
Products unchanged in BIMS since September were never re-examined.

Speed was the only reason to be incremental. With bounded concurrency a full
forced sync was measured at 3.4 minutes (`catalog_pull_seconds: 36.7`,
`stock_fetch_seconds: 59.4`, 11,355 deltas), which fits the schedule. So the
default is now a full comparison on every run, for every tenant (existing ones
included: the migration default is `full`).

The watermark (`last_run_at`, shown by `/sync/{slug}/status`) is **still
recorded** after each successful run; it is just no longer used to filter a
full run.

Switch a tenant back without a deploy:

```bash
curl -X PATCH "$BASE/tenants/<slug>" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"sync_mode": "incremental"}'
```

### What a result says

Every sync result (`POST /sync/{slug}/run` and `last_run_summary`) carries
`mode` (`"full"` or `"incremental"`) next to `scanned` (`{"bims": n, "shopify": n}`),
and the per-SKU `report` repeats it. `mode` is what the run **actually did**:
an incremental tenant with no watermark yet performs a full pull and says
`"full"`. "Nothing changed" (`scanned` large, `matched: 0`) is therefore
distinguishable from "looked at nothing" (`scanned.bims: 0`).

One limit to know about: the diff is against the last stock the system pushed
(stored hashes), not against Shopify's live levels. A full pull fixes products
the incremental filter never returned; it does not by itself re-push a SKU whose
BIMS stock equals the stored hash while Shopify holds something else. Use
`force=true` for that (see `report.unchanged`).

## The safety guard is unchanged

The per-run guard still refuses to push when too much would be zeroed: more than
`max_zero_ratio` (default 20%) of the changed SKUs going to zero, or more than
`max_changed_variants` (default 500) changed SKUs (both tenant-configurable in
`field_mappings`). A tripped guard returns `status: needs_confirmation`, pushes
nothing, and does not advance the watermark. The numbers it evaluated are in
`report.guard`. With full pulls this matters more: a bad full pull could
otherwise zero a whole store in one run.

## Interval

`sync_interval_minutes` is a per-tenant setting (default 30), editable through
the tenant API (`PATCH /tenants/{slug}`, also accepted on create and `PUT`). The
old global `SYNC_INTERVAL_MINUTES` env var is no longer read.

The scheduler keeps one job per active tenant. A change takes effect without a
restart: the API call reschedules the running job immediately, and a reconcile
job (every minute) picks up changes made any other way (new, deactivated or
re-timed tenants). Reconciling only touches a job whose interval changed, so it
never resets a countdown in progress.

### Minimum safe interval (enforced)

A sync that has not finished when the next tick fires is skipped, so an interval
shorter than a run is useless and hammers BIMS. The system therefore measures
itself:

- Every completed (non-dry-run) sync records its duration and mode
  (`sync_states.sync_duration_history`, last 40 entries).
- **Floor = slowest of the last 10 completed runs in the tenant's sync mode x 3,
  rounded up to whole minutes, never below 5 minutes.**
  Example: slowest run 204 s -> 204 x 3 / 60 = 10.2 -> **11 minutes**.
- The factor of 3 means a run occupies at most about a third of its interval, so
  BIMS or Shopify can get up to 3x slower before runs start overlapping.
- With **no history for that mode** (new tenant, or just switched mode) the floor
  is the conservative default of **30 minutes**: the interval production has run
  on, so missing data can never unlock a tighter schedule. It drops to the
  measured floor after the first completed run.

An interval below the floor is **rejected with HTTP 422**, not warned about. The
body states the numbers, e.g.:

```json
{"detail": {
  "message": "sync_interval_minutes=10 is below the minimum safe interval of 11 minutes. The slowest of the last 3 completed full syncs took 204 seconds; 3x that, rounded up, is 11 minutes.",
  "requested_minutes": 10, "min_safe_minutes": 11,
  "slowest_recent_seconds": 204.0, "runs_considered": 3,
  "basis": "measured", "safety_factor": 3, "sync_mode": "full"}}
```

The check runs only when the interval or the mode is being changed, against the
mode the tenant will have afterwards, so unrelated edits are never blocked by an
older low value. Read the current recommendation from
`GET /sync/{slug}/status` -> `sync_interval` (`configured_minutes`,
`min_safe_minutes`, `basis`, `slowest_recent_seconds`, `runs_considered`).

### High-traffic days

Syncs get slower when BIMS is busy, and the floor follows: the window is the 10
most recent runs, so a slow day raises `min_safe_minutes` within a few runs. What
to do:

1. Check `/sync/{slug}/status`. If `min_safe_minutes` is above the configured
   interval, the tenant is already running tighter than measured; raise it with
   `PATCH` (the API will not let you lower it below the floor).
2. Watch `timings.catalog_pull_seconds` / `stock_fetch_seconds` and `duration_seconds`
   in `last_run_summary` to see which phase is slow.
3. If runs regularly overlap (skipped ticks), raise the interval; do not switch
   to incremental to "save time" unless you accept the unreliable `last_update`.
4. Switching mode resets the floor basis to the default (30 min) until a run in
   the new mode completes.
