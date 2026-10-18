# Inventory sync: a continuous pull, one reconciling write cycle per snapshot

How BIMS stock reaches Shopify, how often, and what stops it from going wrong.

## The model

BIMS is the source of truth. Shopify is the mirror. **Every cycle re-verifies the mirror.**

```
BIMS ──(PULL, continuous, paced by its own duration)──▶ catalog snapshot (our DB)
                                                              │ a NEW snapshot lands
                                                              ▼
Shopify ◀──(WRITE CYCLE: read Shopify's real quantities, write what differs)── snapshot
```

| | PULL loop | WRITE CYCLE |
|---|---|---|
| Does | Copies what BIMS reports (products, stock per SKU and per warehouse) into the snapshot | Reads Shopify's real quantity of every variant, compares with the snapshot, writes what differs; then creates what is missing and publishes what qualifies |
| Talks to | BIMS (read-only), Shopify read-only for catalog stats | Shopify. **Never BIMS.** |
| Runs | Back to back: the next pull starts as soon as the previous one ends, no fixed cadence | When, and only when, a pull lands a **new snapshot** |
| Pacing | `pull_min_gap_seconds` (default 60): minimum pause between the END of a pull and the START of the next | Its own duration |
| Overlap | Never overlaps itself; a manual sync holding the pull lock makes the loop stand aside (recorded once, retried after the gap) | Never overlaps itself: a snapshot that lands while the previous cycle still runs is **skipped and recorded, never queued** |

There is no push interval and no reconcile interval, and no fixed pull interval: the
settings `sync_interval_minutes`, `push_interval_minutes` and `reconcile_interval_minutes`
were removed (sending one is refused with a 422 that says so). The only pacing setting is
`pull_min_gap_seconds` (10 to 3600), per tenant and live-editable through
`PATCH /tenants/{slug}`: the API call wakes the running loop, which re-reads the gap on every
pass, so it applies to the wait already in progress and needs no restart.

The minimum gap exists for BIMS's sake. BIMS is the client's live billing ERP; a pull that
answers instantly (an error, an empty page, garbage the sanity guard rejects) must not become
a hot loop, and the service must degrade gracefully when that happens. After a restart the
first pull runs at once if none finished within the gap, so deploys neither hammer BIMS nor
starve the sync.

Because the write cycle reads our own database, BIMS being slow, flaky or down means
Shopify keeps being re-verified against the last good snapshot until that snapshot is older
than `snapshot_max_age_minutes` (see below). A cycle only starts when a pull lands a new
snapshot, so a rejected or failed pull starts nothing.

## Why there is no "last pushed" baseline any more

The write path used to be two paths. A cheap push diffed the snapshot against a **stored
baseline of what it last sent** (`sync_states.product_hashes`); a slow reconcile, three
times a day, diffed against Shopify's real quantities. The baseline was a second notion of
truth, and it drifts: after a half-failed push, a hand edit in the Shopify admin, a mutation
Shopify accepted but did not apply, or a job killed by a deploy, Shopify and the baseline
disagreed, the push concluded "already in sync" forever and the SKU was **frozen** until
someone ran `force`.

Now every cycle reconciles, so the failure mode cannot exist: there is no stored state to
disagree with Shopify. A write that Shopify dropped shows up as drift again on the next
cycle and is retried; nothing is ever "remembered as sent". The baseline column
(`product_hashes`), its refresh logic, the incremental diff (`SyncInventoryToShopify`,
`diff_inventory`), the push worker and its outcome (`last_push_outcome`) were deleted
(migration `0023_reconcile_is_the_write_path`), not left unused. What was kept is the
machinery of the reconcile: `application/reconcile_inventory.py`,
`ShopifyClient.iter_variant_inventory`, the guard and the batch writer.

The price is that every cycle reads the whole Shopify catalog; see [Cost](#cost-per-hour).

## The snapshot

Per tenant, written by the pull (tables `catalog_snapshots`,
`catalog_snapshot_products`, `catalog_snapshot_stock`):

* **Product rows**: BIMS id, `code2`, the SKU matched against Shopify, name,
  `sell_price`, `enabled`, `exclude_ecommerce`, `company_id`. Every row BIMS
  returned is kept, eligible or not, so the snapshot can explain why a product is
  or is not live.
* **Stock per SKU**: the aggregate across the tenant's warehouses (this is what
  gets written) **and the quantity in each warehouse**. With more than one warehouse
  the pull also asks BIMS one warehouse at a time, because the aggregate cannot be
  broken down afterwards; that detail is how "BIMS shows 10 in one warehouse, we
  show 0" gets diagnosed. A SKU BIMS did not return stock for has **no** stock row
  (unknown), it is never stored as 0.
* **Pull metadata**: started/finished timestamps, rows pulled, pages,
  `reported_count`, `complete`, `stop_reason`, warnings, per-phase durations
  (`catalog_pull_seconds`, `stock_fetch_seconds`, `warehouse_stock_seconds`,
  `write_seconds`, `total_seconds`) and the catalog stats computed by the pull.

### How a snapshot is written (and why a half-written one is never visible)

A pull is never written over the rows a write cycle is reading. It is written as a
separate `building` generation (its own `snapshot_id`), in chunks, and only then
made visible by flipping `is_current` in **one short transaction**. A partial unique
index guarantees one current snapshot per tenant. So:

* a write cycle reads by the id of the current snapshot, and a `building` generation is
  never current: it sees the old snapshot or the whole new one, nothing in between;
* a failure, or a container killed mid-write, leaves an invisible `building`
  orphan that the next pull deletes (status `failed`, reason `abandoned`);
* the previous generation keeps its rows until the *next* flip, so a cycle that
  picked it just before a flip can finish reading it (a cycle also loads its snapshot into
  memory once, at the start).

Old generations' rows are purged (current and previous are kept); the metadata
history (last 50) is kept.

### Reading the snapshot metadata

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/sync/<slug>/snapshot"
```

* `current`: the snapshot a write cycle would use. `finished_at`, `age_minutes`,
  `rows_pulled`, `stock_rows`, `pages`, `reported_count`, `complete` (`true`,
  `false`, or `null` when BIMS sent no count), `stop_reason`, `warnings`, `timings`,
  `stats`. `null` before the first pull lands.
* `usable_for_write` / `decline`: whether a write cycle would use it right now, and if not
  the reason (`missing`, `incomplete`, `stale`) with the numbers.
* `history`: the last 10 pulls, newest first, **including rejected ones**
  (`status: "rejected"` with `rejection`).
* `last_pull` / `last_reconcile` / `last_heal`: what the most recent pull, stock pass and
  self-healing stages did.
* `settings`: the tenant settings that govern the workers.

`GET /sync/<slug>/status` carries `last_pull_outcome` and `last_reconcile_outcome` too, plus
`pull` and `write_cycle` (how long each really takes), and the portal dashboard shows the
snapshot age and both outcomes (`workers.pull`, `workers.write`).

Worker outcomes (`status`): pull is `ok`, `rejected`, `skipped`, `failed`; the write cycle is
`ok`, `declined`, `needs_confirmation`, `failed`. A skipped pull keeps the last real outcome
under `previous` and counts `consecutive_skips`; a skipped write cycle is noted under
`last_skip` (with `skipped_total`) without replacing the write outcome. Audit log actions:
`snapshot.pull_completed`, `snapshot.pull_rejected`, `snapshot.pull_failed`,
`snapshot.pull_skipped`, and the `sync.*` ones listed under [Drift](#drift-the-per-cycle-health-number).

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

### Write cycle: refuses to write from a snapshot it cannot trust

The stock pass declines, records why (`last_reconcile_outcome`, audit
`sync.reconcile_declined`) and leaves Shopify alone when the snapshot is:

* `missing`: no pull has landed yet;
* `incomplete`: flagged `complete: false` (the pull worker never stores such a
  snapshot; this guards anything else that might);
* `stale`: finished longer ago than the tenant's `snapshot_max_age_minutes`
  (default 120). Writing from a stale snapshot is worse than not writing.

The snapshot is judged before Shopify is read and **again right before anything is
written**, because reading a whole catalog takes minutes. The create and publish stages apply
the same rule.

The API refuses a limit that the measured runs can never satisfy: `snapshot_max_age_minutes`
must be at least 2 x the slowest recent pull plus the minimum gap (and 2 x the slowest recent
write cycle), so one failed, rejected or skipped pull does not stop writing and a third
consecutive miss does. With no measured history yet the limit is not second-guessed.

### The safety rules, now guarding every cycle

They matter more than before: every cycle writes from the snapshot, so a bad pull has a
bigger blast radius. All of them are enforced inside `do_reconcile`:

* **Mass-zero / max-changed guard**: more than `max_zero_ratio` (default 20%) of the SKUs to
  change going to zero, or more than `max_changed_variants` (default 500) SKUs to change
  (both tenant-configurable in `field_mappings`) returns `status: needs_confirmation`,
  writes nothing and does not advance `last_run_at`. The numbers it evaluated are in
  `report.guard`. The drift is still reported. `force` is the explicit operator override.
  Create and publish decline on a snapshot the guard flagged (`stock_guard_fired`).
* **Absent BIMS stock is unknown, never zero**: a SKU BIMS did not mention has no stock in the
  snapshot, is left alone in Shopify and is only counted (`unknown_stock`), unless the tenant
  opts in with `field_mappings.absent_stock_means_zero`. **A SKU BIMS reports as 0 IS written
  as 0**: if BIMS says there is nothing, Shopify is told there is nothing.
* **Stock consolidation only through `consolidate_sellable_stock`**, at pull time. The write
  path has no second implementation.
* **Integers.** Quantities are written as integers, so the comparison is made on the integer
  that would be written: BIMS `2.5` against Shopify `2` is not drift.
* **Pinned snapshot.** A cycle reads the current snapshot once, when it starts, and every
  stage (both stock passes, create, publish) works from that one. A stock pass takes minutes,
  and a newer snapshot landing meanwhile must not slip past the guard verdict the first pass
  gave its predecessor.
* **Create/publish guards** (more than `max_create_ratio` of the catalog "missing" is refused,
  at most `max_create_per_cycle` product groups per cycle): unchanged, see below.

A variant with **no active inventory level at the location** (Shopify returns `null` for a
never-stocked or deactivated level) is not a variant holding zero, and the two are kept apart:

| Snapshot | Shopify | Result |
|---|---|---|
| 5 | no level | written (the write stocks the item at the location); counted under `no_level.pushed`, not under `drift` |
| 0 | no level | **not written**: a zero would invent a level that was never stocked; counted under `no_level.skipped_zero` |
| 0 | level, 5 | written as 0, drift |
| 0 | level, 0 | in sync |
| 3 | level, 0 | drift (`shopify: 0`) |

## Drift: the per-cycle health number

`last_reconcile` in `GET /sync/{slug}/snapshot` (and `last_reconcile_outcome` in
`GET /sync/{slug}/status`) is recorded by **every** cycle:

```json
"last_reconcile": {
  "status": "ok",
  "drift": {
    "count": 12,              // SKUs whose real Shopify quantity differed from the snapshot
    "total_abs_delta": 87,    // units out, summed
    "max_abs_delta": 30,
    "sample": [{"sku": "A1", "shopify": 2, "snapshot": 5, "delta": 3}]   // bounded (20)
  },
  "updated": 15, "in_sync": 12606,
  "no_level": {"count": 3, "pushed": 3, "skipped_zero": 0},
  "unknown_stock": 4, "not_in_shopify": 9,
  "scanned": {"snapshot": 12621, "shopify": 12630},
  "duration_seconds": 412.3, "trigger": "scheduled",
  "last_skip": {"status": "skipped", "skipped_total": 2}   // snapshots a running cycle had to skip
}
```

`drift.count` used to be the result of an occasional safety-net check. It is now what each
cycle found wrong *before* it fixed it, which makes it the health metric of the whole sync:

* **Healthy: close to zero.** A unit sold in Shopify moves its `available` down at once, while
  BIMS only reflects the sale at the next pull, so a few SKUs sold since the snapshot legitimately
  differ (they are the orders of the last pull period plus BIMS-side changes, and the write fixes
  them). Hundreds, with BIMS quiet, is not normal.
* **A count that stays high or rises cycle after cycle**: something upstream drops writes. The
  same SKUs reappearing in `drift.sample` after being corrected point at a mutation Shopify
  accepts but does not apply; a jump right after a deploy points at a killed cycle; `failed`
  samples and the `sync.reconcile_error` audit entries show write errors.
* `status` is `ok`, `needs_confirmation` (guard fired, nothing written), `declined` (no usable
  snapshot), `failed`, with `last_skip` alongside the last real outcome.

Audit log: a scheduled cycle that found nothing to fix is **not** audited (its outcome above
is overwritten every cycle; one audit row per ten minutes would bury the real ones). A cycle
that drifted or wrote, a declined or guard-blocked one, a failure, and every manual run are
(`sync.reconcile_completed`, `sync.reconcile_declined`, `sync.reconcile_needs_confirmation`,
`sync.reconcile_error`, `sync.dry_run`).

## The one-shot: `POST /sync/{slug}/run` and the `sync` ops command

Implemented as **pull now, then reconcile now** over exactly the same two functions the
scheduler runs (not a separate code path):

* query/options: `dry_run`, `force`; `full` is accepted and ignored (every pull is full);
* it takes both locks (409 if either is running), so it cannot overlap the pull loop or a
  write cycle;
* a **rejected pull stops it**: `status: "pull_rejected"` with the reason, nothing is written
  (the `refresh` ops command stops there too, before `publish`);
* a dry run still refreshes the snapshot (a read of BIMS into our database, not a write to
  Shopify) and reports `would_update`, `drift` and `guard_fired`;
* the result is the reconcile's (`status`, `mode: "reconcile"`, `updated`, `drift`, `report`,
  `scanned`, `timings`) plus `pull` and `snapshot`. `timings.catalog_pull_seconds` /
  `stock_fetch_seconds` are the pull's, `reconcile_seconds` the write's.

The `reconcile` ops command (`POST /ops/{slug}/run {"command": "reconcile", ...}`, options
`dry_run`, `force`) is the same `do_reconcile` the cycle runs, holding the same write lock.

### Which commands read cached data and which are live

| Command | Data | Note |
|---|---|---|
| scheduled write cycle, one-shot write half | snapshot | the whole point |
| `status` | BIMS half from the **snapshot**, Shopify half live | `source` and `snapshot.age_minutes` in the report; `options.live: true` / `--live` pulls BIMS now |
| portal dashboard, `GET /sync/{slug}/reconciliation` | numbers **recomputed by every pull** | no longer waits for someone to run `status` |
| `publish` | **live** BIMS | its stock gate must be fresh; deliberately not switched |
| `diagnose` | **live** BIMS | it explains a live discrepancy; deliberately not switched |
| `import`, `cleanup_no_stock` | live BIMS | not switched |

## Locking

Two per-tenant locks, taken by the scheduler, the HTTP endpoint and the ops job runner alike:

* the **pull** lock: held by every pull (the loop and the one-shot);
* the **write** lock: held by every write cycle (stock, create, publish, the second stock pass),
  the one-shot's write half and the `reconcile` ops command.

The one-shot `sync` takes both (pull first, always, so it cannot deadlock). The pull and the
write cycle deliberately do **not** exclude each other: a pull only lands a new snapshot, and
a cycle keeps working from the snapshot it started with. Each excludes itself, and a manual
operation excludes both.


## Cost per hour

Numbers below are structural (from the code and the docs' own figures: ~28,000 variants, ~280
to 340 requests of 100 variants, a pull of about 10 minutes); they are **not measured on
production**. The system now records what it really takes: `GET /sync/{slug}/status` ->
`pull` and `write_cycle` (`slowest_recent_seconds`, `latest_seconds`).

| | Before (30 min pull interval, 5 min push, 3 reconciles/day) | Now (continuous pull, write cycle per snapshot) |
|---|---|---|
| BIMS: full pulls per hour | 2 | about 5.5 (a 10 minute pull + the 60 s gap) |
| Shopify: catalog scans per hour | about 4 (the pull's catalog stats + the push's SKU map, once per snapshot) + 3/24 reconciles | about 11 (one per pull for the catalog stats, one per write cycle) |
| Shopify: stock writes | only what differed | only what differed (unchanged) |

BIMS sees roughly 2.7x the load it saw at the 30 minute cadence, which is inherent to pulling
continuously; the concurrency of a pull is unchanged. Shopify sees roughly 2.7x the read
requests, and the cycle's read is the heavier query (it selects the inventory level of each
variant). Shopify's throttling is cost-based, so this is where the cycle can run slower than
expected: a store plan with a low restore rate makes the same 280 pages take several times
longer.

### Is one write cycle per snapshot sustainable?

It depends on one number, the write cycle's duration against the pull period (the pull's
duration plus the gap, about 11 minutes):

* **cycle shorter than the pull period**: every snapshot gets its own cycle. Shopify is
  re-verified roughly every 11 minutes.
* **cycle longer than the pull period**: the cycle that is running when the next snapshot lands
  makes that snapshot be **skipped** (`last_reconcile.last_skip.skipped_total`, a
  `worker_run_skipped` warning in the log). Nothing queues, nothing overlaps; the cycle simply
  runs as often as it can, on the newest snapshot available when it frees up. A cycle taking 25
  minutes against an 11 minute period verifies about every 30 minutes with about 2 of 3
  snapshots skipped. That is graceful, but it is not "a cycle per snapshot" any more, and the
  skip counter says so.

If skips are constant, the options are a longer minimum gap (fewer, rarer snapshots for the
same cycle), or making the cycle's read cheaper (see the next section).

### Known inefficiency, not changed

The pull still computes the dashboard's catalog stats with its own full Shopify variant
listing, so every pull and every cycle each scan the catalog once. With a pull every 11
minutes that second scan (which also lengthens the pull) is now the most wasteful part. It was
out of scope here; computing those stats from the cycle's read instead is the obvious next step.

## The self-healing stages of the write cycle: the store follows BIMS by itself

The write cycle does more than stock. Besides stock, it closes the gap between what BIMS
has in stock and what the store holds, with nobody running anything by hand.
Everything is judged from the persisted snapshot; BIMS is never called.

1. **Stock** (`do_reconcile`). It goes first on purpose: its
   mass-zero / max-changed guard judges the snapshot before anything is
   created, and a snapshot the guard flagged creates and publishes nothing.
2. **Create what is missing** (`auto_create_missing`). A group (product) with
   confirmed stock in BIMS and no SKU in the store is created through
   `productSet`, as a DRAFT. A size added in BIMS to a product already in the
   store is added to THAT product through `productVariantsBulkCreate`, tracked
   and activated at the tenant location. A size value the product already has
   under another SKU is a **conflict**: it is recorded and never forced. If it
   created anything, a second stock pass runs so the new variants receive their stock
   (Shopify holds no inventory level for them yet, so the pass writes it).
3. **Publish what qualifies** (`auto_publish`). Drafts that pass the manual
   `publish` gates, unchanged (an image unless the tenant sets
   `field_mappings.publish_require_images=false`, confirmed stock above zero on
   some SKU, every SKU known to BIMS) are activated and put on the Online Store
   channel. This runs after the stock passes, so a product is never public while
   it shows zero.

Both stages default ON per tenant and can be switched off with
`PATCH /tenants/{slug}` (`{"auto_create_missing": false}` /
`{"auto_publish": false}`): no deploy, no restart. The stock pass is not a stage
and always runs.

### What it refuses to do

* No usable snapshot (missing, incomplete, stale): both stages decline, same
  rule as the stock pass, and Shopify is not touched.
* The stock guard fired on this snapshot: both stages decline (`stock_guard_fired`).
* A SKU that is not `code2` (`field_mappings.product_sku_field`): the create
  stage declines, because the import names variants after `code2`.
* Stock absent from the BIMS answer is UNKNOWN, never zero: such a product is
  not created and not published. Stock comes from the snapshot, which was
  consolidated with `consolidate_sellable_stock` at pull time.
* Most of what BIMS has in stock "missing" from a store that has a real catalog
  (more than `field_mappings.max_create_ratio`, default 0.5, once at least 50
  SKUs match) is `needs_confirmation`: nothing is written. Use the manual
  `import` after looking at why.
* At most `field_mappings.max_create_per_cycle` (default 300) product groups are
  written per cycle; the rest continue on the next cycle.
* The whole cycle runs under the write lock, so a manual sync or a `reconcile` ops command
  stands aside (and a snapshot landing meanwhile is skipped).

### Cost

A healthy steady state does almost nothing. The create stage remembers which SKUs
Shopify held at its last scan (`sync_states.shopify_sku_index`):

| Situation | Shopify calls from the stages |
| --- | --- |
| Same snapshot as the last cycle (a manual re-run) | 0 |
| New snapshot, every wanted SKU already known | create: 0; publish: 1 draft listing (a page per 100 drafts) |
| New BIMS SKU or a failed creation to retry | one full variant scan, then one write per product group |
| Every 6 hours | one full variant scan (picks up products deleted in the admin) |
| A cycle that created something | + a second stock pass (one more catalog read) and the publish writes |

Conflicts are parked in the index so they do not trigger a scan on every pull.

### Reading what it did

`GET /sync/{slug}/snapshot` returns `last_heal` with one entry per stage, each
with the snapshot it judged and when: `create` (`created`, `variants_added`,
`conflicts`, `failed`, `deferred`) and `publish` (`published`, `skipped_no_images`,
`skipped_no_stock`, `unknown`). `GET /sync/{slug}/status` carries the same under
`last_heal_outcome`. Each action is also audited (`sync.create_ok`,
`sync.create_needs_confirmation`, `sync.publish_completed`, ...) and a cycle that
healed something logs `scheduled_cycle_healed` with the counts.

## Settings

* `pull_min_gap_seconds` (default 60, 10 to 3600): see [The model](#the-model).
* `snapshot_max_age_minutes` (default 120) and `pull_max_drop_ratio` (default 0.30): the
  staleness limit and the pull sanity guard.
* `auto_create_missing`, `auto_publish` (default on): the two self-healing stages.
* The old global `SYNC_INTERVAL_MINUTES` env var is not read, and `sync_interval_minutes`,
  `push_interval_minutes` and `reconcile_interval_minutes` no longer exist (migration
  `0024_continuous_pull_settings` drops the columns; their values are not carried over, since
  each described a timer that is gone, so every tenant starts on the default gap).

All of them are per tenant, edited through the tenant API (`PATCH /tenants/{slug}`, also
accepted on create and `PUT`), and picked up **without a restart**.

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
2. Meanwhile nothing is broken: the previous snapshot stays current and the write cycle keeps
   being possible from it only until it passes `snapshot_max_age_minutes`. No new snapshot lands, so
   no cycle starts until one does.
3. The pull loop tries again by itself after the minimum gap. A transient BIMS partial answer
   (the 33,705 / 11,355 case) usually resolves on the next one.
4. If it keeps being rejected, decide whether BIMS really lost data:
   * **BIMS is wrong** (a truncated answer): wait, or check BIMS; do nothing to the
     settings.
   * **The catalog legitimately shrank** (a large deliberate clean-up in BIMS): raise
     `pull_max_drop_ratio` for one pull (`PATCH /tenants/<slug>` with
     `{"pull_max_drop_ratio": 0.9}`), run `POST /sync/<slug>/run?dry_run=true` or wait
     for the next pull, then set it back.
5. If rejections last longer than `snapshot_max_age_minutes` the write cycle will
   **decline as stale** (`last_reconcile.reason == "stale"`). That is the intended
   fail-safe, not a bug; fix the pull, do not raise the limit to silence it.

## High-traffic days

Pulls get slower when BIMS is busy; the loop paces itself by that, so nothing needs raising.

1. Check `GET /sync/{slug}/status` -> `pull.slowest_recent_seconds` and
   `write_cycle.slowest_recent_seconds`. If `snapshot_max_age_minutes` is below twice the pull
   period the API refuses it; raise it with `PATCH`.
2. Watch `current.timings` in `/sync/{slug}/snapshot` to see which phase is slow.
3. If cycles are being skipped (`last_reconcile.last_skip.skipped_total` keeps growing), the
   write cycle is slower than the pull period: see [Is one write cycle per snapshot
   sustainable?](#is-one-write-cycle-per-snapshot-sustainable). To give BIMS more room, raise
   `pull_min_gap_seconds`.
