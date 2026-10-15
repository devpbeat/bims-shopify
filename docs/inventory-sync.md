# Inventory sync: two workers over a persisted snapshot

How BIMS stock reaches Shopify, how often, and what stops it from going wrong.

## The two-worker model

The sync used to be one synchronous job: pull the whole BIMS catalog, fetch stock,
diff, push to Shopify, all in one run. While BIMS was slow (3.4 to 16 minutes)
Shopify got nothing, a container restart during that window lost the whole run,
and every command pulled the whole catalog again.

It is now two independent workers per tenant with a database table between them:

```
BIMS ──(PULL, slow, own cadence)──▶ catalog snapshot (our DB) ──(PUSH, seconds, own cadence)──▶ Shopify
```

| | PULL worker | PUSH worker |
|---|---|---|
| Does | Copies what BIMS reports (products, stock per SKU and per warehouse) into the snapshot | Diffs the snapshot against what was last pushed, pushes only the differences |
| Talks to | BIMS (read-only), Shopify read-only for catalog stats | Shopify. **Never BIMS.** |
| Takes | As long as BIMS takes (minutes) | Seconds |
| Interval | `sync_interval_minutes` (default 30) | `push_interval_minutes` (default 5) |
| Overlap | Never overlaps itself; a tick that finds the previous pull running is **skipped and recorded**, not queued | Same, independently |
| Blocks the other? | No | No: a pull in progress does not stop the push |

Because the push reads our own database, BIMS being slow, flaky or down no longer
means Shopify gets nothing: the push keeps working from the last good snapshot
until that snapshot is older than `snapshot_max_age_minutes` (see below).

Both intervals and the staleness limit are per-tenant, edited through the tenant
API (`PATCH /tenants/{slug}`, also accepted on create and `PUT`), and picked up
**without a restart**: the API call reschedules the running jobs immediately and a
reconcile job (every minute) catches changes made any other way.

After a restart the first pull runs immediately if there is no snapshot or it is
overdue, and otherwise waits until `last pull + interval`, so deploys neither
re-pull a fresh snapshot nor starve an old one.

## The snapshot

Per tenant, written by the pull (tables `catalog_snapshots`,
`catalog_snapshot_products`, `catalog_snapshot_stock`):

* **Product rows**: BIMS id, `code2`, the SKU matched against Shopify, name,
  `sell_price`, `enabled`, `exclude_ecommerce`, `company_id`. Every row BIMS
  returned is kept, eligible or not, so the snapshot can explain why a product is
  or is not live.
* **Stock per SKU**: the aggregate across the tenant's warehouses (this is what
  gets pushed) **and the quantity in each warehouse**. With more than one warehouse
  the pull also asks BIMS one warehouse at a time, because the aggregate cannot be
  broken down afterwards; that detail is how "BIMS shows 10 in one warehouse, we
  show 0" gets diagnosed. A SKU BIMS did not return stock for has **no** stock row
  (unknown), it is never stored as 0.
* **Pull metadata**: started/finished timestamps, rows pulled, pages,
  `reported_count`, `complete`, `stop_reason`, warnings, per-phase durations
  (`catalog_pull_seconds`, `stock_fetch_seconds`, `warehouse_stock_seconds`,
  `write_seconds`, `total_seconds`) and the catalog stats computed by the pull.

### How a snapshot is written (and why a half-written one is never visible)

A pull is never written over the rows the push is reading. It is written as a
separate `building` generation (its own `snapshot_id`), in chunks, and only then
made visible by flipping `is_current` in **one short transaction**. A partial unique
index guarantees one current snapshot per tenant. So:

* the push reads by the id of the current snapshot, and a `building` generation is
  never current: it sees the old snapshot or the whole new one, nothing in between;
* a failure, or a container killed mid-write, leaves an invisible `building`
  orphan that the next pull deletes (status `failed`, reason `abandoned`);
* the previous generation keeps its rows until the *next* flip, so a push that
  picked it just before a flip can finish reading it.

Old generations' rows are purged (current and previous are kept); the metadata
history (last 50) is kept.

### Reading the snapshot metadata

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/sync/<slug>/snapshot"
```

* `current`: the snapshot the push would use. `finished_at`, `age_minutes`,
  `rows_pulled`, `stock_rows`, `pages`, `reported_count`, `complete` (`true`,
  `false`, or `null` when BIMS sent no count), `stop_reason`, `warnings`, `timings`,
  `stats`. `null` before the first pull lands.
* `usable_for_push` / `decline`: whether the push would use it right now, and if not
  the reason (`missing`, `incomplete`, `stale`) with the numbers.
* `history`: the last 10 pulls, newest first, **including rejected ones**
  (`status: "rejected"` with `rejection`).
* `last_pull` / `last_push`: what the most recent tick of each worker did.
* `settings`: the four tenant settings that govern the workers.

`GET /sync/<slug>/status` carries `last_pull_outcome` and `last_push_outcome` too,
and the portal dashboard shows the snapshot age and both outcomes.

Worker outcomes (`status`): pull is `ok`, `rejected`, `skipped`, `failed`; push is
`ok`, `declined`, `needs_confirmation`, `failed`. A skipped pull keeps the last real
outcome under `previous` and counts `consecutive_skips`; a skipped push tick is noted
under `last_skip` without replacing the push outcome. Audit log actions: `snapshot.pull_completed`,
`snapshot.pull_rejected`, `snapshot.pull_failed`, `snapshot.pull_skipped`,
`sync.completed`, `sync.push_declined`, `sync.needs_confirmation`, `sync.error`.
Repeats of an identical decline or guard block are audited once, not once per tick.

## Guards

### Pull: the sanity guard

BIMS has returned **33,705 rows and 11,355 rows for the same full pull hours
apart**. A pull is rejected, the previous snapshot is kept, and the rejection is
recorded loudly (error log `catalog_pull_rejected`, audit `snapshot.pull_rejected`,
`last_error` on the sync status, a `rejected` entry in the snapshot history) when:

* BIMS itself says it is incomplete (`complete` is explicitly `false`), whatever
  its size. This also rejects a first pull. `complete: null` (no count sent) is not a
  rejection; the size check still applies.
* it returns **more than `pull_max_drop_ratio` fewer rows** than the snapshot it
  would replace (default `0.30`, a tenant setting). 33,705 to 11,355 is a 66% drop and
  is refused; 100 to 70 rows (exactly 30%) is accepted, 100 to 69 is not. The same
  check applies to the number of SKUs BIMS returned stock for (`too_few_stock_rows`).
  `1.0` disables the size check.

A growing catalog is never rejected. The first pull has nothing to compare against.

### Push: refuses to push from a snapshot it cannot trust

The push declines, records why (`last_push_outcome`, audit `sync.push_declined`) and
leaves Shopify alone when the snapshot is:

* `missing`: no pull has landed yet;
* `incomplete`: flagged `complete: false` (the pull worker never stores such a
  snapshot; this guards anything else that might);
* `stale`: finished longer ago than the tenant's `snapshot_max_age_minutes`
  (default 120). Pushing from a stale snapshot is worse than not pushing.

The API refuses a limit smaller than 2 x the pull interval: that way one failed,
rejected or skipped pull does not stop the push, and a third consecutive miss does.

### The earlier safety rules are unchanged

They live in the same push use case as before and run in the push:

* **Mass-zero / max-changed guard**: more than `max_zero_ratio` (default 20%) of the
  changed SKUs going to zero, or more than `max_changed_variants` (default 500)
  changed SKUs (both tenant-configurable in `field_mappings`) returns
  `status: needs_confirmation`, pushes nothing and does not advance the watermark.
  The numbers it evaluated are in `report.guard`.
* **Absent stock is unknown, never zero**: a SKU BIMS returned no stock for is
  skipped (and listed under `report.not_in_bims`), unless the tenant opts in with
  `field_mappings.absent_stock_means_zero`. The rule is one shared function used by
  both the live adapter and the snapshot reader.
* **The per-SKU detail report** (`updated`, `unchanged`, `zero_stock`,
  `not_in_shopify`, `not_in_bims`, `failed`, `blocked_by_guard`, `guard`, `scanned`)
  now describes the push: it is computed from the snapshot against Shopify's variant
  list, always as a full comparison.
* **The diff is against what was last pushed**, not against Shopify's live levels.
  Use `force=true` to re-push a SKU whose BIMS stock equals the stored baseline but
  whose Shopify level is different (see `report.unchanged`).

A scheduled push tick whose previous push of the *same snapshot* succeeded does
nothing (`unchanged`, no Shopify call), so it can tick every few minutes cheaply.
Consequence: a SKU that only becomes matchable in Shopify later (a product imported
after the push) gets its stock at the next snapshot, or immediately with the one-shot.

## The one-shot: `POST /sync/{slug}/run` and the `sync` ops command

Unchanged for operators, implemented as **pull now, then push now** over exactly the
same two functions the scheduler runs (not a separate code path):

* query/options: `dry_run`, `force` as before; `full` is accepted and ignored (every
  pull is full);
* it takes both workers' locks (409 if either is running), so it cannot overlap them;
* a **rejected pull stops it**: `status: "pull_rejected"` with the reason, nothing is
  pushed (the `refresh` ops command stops there too, before `publish`);
* a dry run still refreshes the snapshot: that is a read of BIMS into our database,
  not a write to Shopify;
* the push always compares (no "unchanged" shortcut). The result keeps its old shape
  (`status`, `mode`, `synced_deltas`, `timings`, `scanned`, `report`) plus `pull` and
  `snapshot`. `timings.catalog_pull_seconds` / `stock_fetch_seconds` are the pull's,
  `push_seconds` the push's.

### Which commands read cached data and which are live

| Command | Data | Note |
|---|---|---|
| scheduled PUSH, one-shot push half | snapshot | the whole point |
| `status` | BIMS half from the **snapshot**, Shopify half live | `source` and `snapshot.age_minutes` in the report; `options.live: true` / `--live` pulls BIMS now |
| portal dashboard, `GET /sync/{slug}/reconciliation` | numbers **recomputed by every pull** | no longer waits for someone to run `status` |
| `publish` | **live** BIMS | its stock gate must be fresh; deliberately not switched |
| `diagnose` | **live** BIMS | it explains a live discrepancy; deliberately not switched |
| `import`, `cleanup_no_stock` | live BIMS | not switched |

## Intervals

`sync_interval_minutes` is the **pull** interval (default 30, the name predates the
split); `push_interval_minutes` is the push interval (default 5). The old global
`SYNC_INTERVAL_MINUTES` env var is not read.

### Minimum safe interval (enforced, measured separately)

A tick that finds the previous run still going is skipped, so an interval shorter
than a run is useless and hammers BIMS. The system measures itself, one history per
worker (`sync_states.sync_duration_history`, last 40 per worker, window of 10):

* **Pull** floor: slowest of the last 10 completed pulls (accepted or rejected) x 3,
  rounded up to whole minutes, never below 5; with no history, 30 minutes.
* **Push** floor: the same rule on completed pushes, never below 1 minute; with no
  history, 1 minute.

The histories are kept apart on purpose: a push takes seconds, and mixing it into the
pull's history would let the pull interval drop far below what BIMS sustains.

An interval below the floor is **rejected with HTTP 422** whose body states the
numbers. The check runs only when that interval is being changed. Read the current
recommendations from `GET /sync/{slug}/status` -> `sync_interval` and `push_interval`.

### `sync_mode` is deprecated

`full` vs `incremental` no longer changes what the workers do: **every pull is a full
pull**. A partial pull cannot replace a snapshot, and stock changes do not touch a
product's `last_update`, so an incremental merge would keep stale stock for every
unchanged row (the exact failure the full default was introduced to fix). The setting
is still stored and accepted by the API so existing clients keep working.

## When a pull is rejected: what to do

1. `GET /sync/<slug>/snapshot`: find the `rejected` entry in `history` (or `last_pull`).
   `rejection.reason` is `incomplete`, `too_few_rows` or `too_few_stock_rows` with the
   numbers (`rows`, `previous_rows`, `drop_ratio`, `max_drop_ratio`, or
   `reported_count`, `stop_reason`, `warnings`).
2. Meanwhile nothing is broken: the previous snapshot stays current and the push keeps
   working from it until it passes `snapshot_max_age_minutes`.
3. The next scheduled pull tries again by itself. A transient BIMS partial answer
   (the 33,705 / 11,355 case) usually resolves on the next one.
4. If it keeps being rejected, decide whether BIMS really lost data:
   * **BIMS is wrong** (a truncated answer): wait, or check BIMS; do nothing to the
     settings.
   * **The catalog legitimately shrank** (a large deliberate clean-up in BIMS): raise
     `pull_max_drop_ratio` for one pull (`PATCH /tenants/<slug>` with
     `{"pull_max_drop_ratio": 0.9}`), run `POST /sync/<slug>/run?dry_run=true` or wait
     for the next pull, then set it back.
5. If rejections last longer than `snapshot_max_age_minutes` the push starts
   **declining as stale** (`last_push.reason == "stale"`). That is the intended
   fail-safe, not a bug; fix the pull, do not raise the limit to silence it.

## High-traffic days

Pulls get slower when BIMS is busy and the pull floor follows within a few runs.

1. Check `GET /sync/{slug}/status`. If `sync_interval.min_safe_minutes` is above the
   configured interval, raise it with `PATCH` (the API will not let you lower it
   below the floor).
2. Watch `current.timings` in `/sync/{slug}/snapshot` to see which phase is slow.
3. If pulls are being skipped (`last_pull.status == "skipped"`), raise the pull
   interval. The push is unaffected by a slow pull.
