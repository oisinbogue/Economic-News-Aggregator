# Indicators subsystem

Config-schema and operations reference for `indicators/` and
`config/indicators/`. See [`INDICATORS_SCHEMA_PROPOSAL.md`](INDICATORS_SCHEMA_PROPOSAL.md)
for the original design rationale and the full `indicators.db` schema; this
document is the day-to-day "how do I add a country / indicator / provider"
guide.

**Status:** Phase 1 (foundation) + Phase 2 (site output) + Phase 3 complete
(release calendar, release-aware fetch, rate-limit safety, revision
logging, the scheduled `indicators.yml` workflow and its db backup/restore
-- see "Operations" below). Five countries: US, UK, Ireland, EU (euro area),
Germany. No country extras
yet (Phase 4), no countries beyond the five above (Phase 5+). The
indicators subsystem is a separate failure domain from the news pipeline --
separate package, separate db file (`data/indicators.db`), separate GitHub
Actions workflows. Nothing here touches `pipeline/` or the site templates.

---

## Config layout

```
config/indicators/
  countries.yaml       # every tracked country/bloc
  core.yaml            # the 16 core indicator definitions, country-agnostic
  series/
    us.yaml             # one file per country -- provider mapping per indicator
    gb.yaml
    ie.yaml
    eu.yaml
    de.yaml
```

Loaded by `indicators/config.py` (`get_countries()`, `get_core_indicators()`,
`get_series(iso)`), each `@lru_cache`d for the life of the process. There is
no reload hatch -- a process must restart to pick up a config edit (fine for
one-shot `python -m indicators.*` invocations; tests clear the caches
explicitly via `monkeypatch` + `.cache_clear()`, see any `tests/test_indicators_*.py`).

### `countries.yaml`

```yaml
countries:
  <iso>:
    iso: <iso>              # alpha-2 code, also the series/<iso>.yaml filename
    name: <display name>
    currency: <ISO 4217>
    timezone: <IANA tz>
```

`iso` doubles as the `series_id` prefix (`ie.cpi_headline`) and the
`series/<iso>.yaml` filename. `eu` is a bloc, not a country, but is treated
identically everywhere in the schema.

### `core.yaml`

One entry per core indicator key (see brief §6.1 for the canonical 16),
country-agnostic:

```yaml
indicators:
  <indicator_key>:
    topic: economy | prices | labour | rates | housing | confidence |
           government | trade | energy | markets
    unit: percent | index | currency | ratio | count | percent_gdp | bps
    frequency: D | W | M | Q | A
    transform_default: yoy | mom | qoq | level | annualised
    direction_good: up | down | neutral
    target_band: [low, high]   # optional -- e.g. inflation's ~1-3% band
```

A country's `series/<iso>.yaml` entry for the same key can override any of
`unit`/`frequency`/`transform_default`/`direction_good`/`target_band` (e.g. a
country whose inflation target genuinely differs); `indicators/db.py`'s
`sync_series_from_config()` resolves country-over-core before writing the
`series` table row.

### `series/<iso>.yaml`

```yaml
country: <iso>
series:
  <indicator_key>:                      # must match a core.yaml key
    provider: dbnomics                  # which indicators/providers/*.py module
    provider_series_id: <provider-specific id>
    fallback_providers: [<name>, ...]   # not yet consulted by fetch.py -- recorded for a later phase
    source_agency: <string>             # attribution: who produced the data
    source_url: <string>
    licence: <string>
    seasonal_adjustment: sa | nsa | unknown
    revisable: true | false
    notes: <string>                     # optional, surfaced as a UI tooltip later
    news_topic_tags: [<THEME>, ...]     # must be keys from config/taxonomy.yaml's `keywords:`
    release_schedule: <rule string>     # optional -- see "Release schedule grammar" below
    release_lag_days: <int>             # optional -- typical publication lag, informational
  # ...one entry per core.yaml key, every one of them, no exceptions...
extras: []                              # country-specific extras, populated Phase 4
```

`release_schedule` and `release_lag_days` are both optional (nullable in
the db). Populate them wherever the real-world release calendar is
genuinely knowable; where it isn't (an irregular meeting-based rate
decision, a biannual IMF WEO vintage, an OECD aggregate with no published
release-day commitment), leave them unset and add a
`# release_schedule: unknown -- <reason>` comment instead of guessing --
every current `series/<iso>.yaml` follows this convention, so `grep
"release_schedule: unknown"` finds every deliberately-unscheduled series
and why.

**Every `core.yaml` key must appear** in every `series/<iso>.yaml`, either
as a real provider mapping (above) or as an explicit unavailable marker:

```yaml
  <indicator_key>:
    unavailable: true
    unavailable_reason: <why -- shown as-is, be specific>
    news_topic_tags: [...]
```

`sync_series_from_config()` still inserts an unavailable entry into the
`series` table (`active = 0`, `unavailable_reason` set) rather than
skipping it, so the gap is visible in the db, not silently missing.

**Providers implemented:** `dbnomics`, `ecb`, `eurostat`, `ons`, `boe`,
`stooq`, `bls`, `bis`, and `oecd` (`indicators/providers/*.py`), covering
every provider referenced across the five Phase-1 countries'
`series/*.yaml`. `bls` (US Bureau of Labor Statistics public API), `bis`
(Bank for International Settlements Statistics API), and `oecd` (OECD
SDMX Data API) were added 2026-08-13 to migrate series off DBnomics
mirrors that had stopped tracking upstream:
`us.cpi_headline`/`cpi_core`/`unemployment_rate`/`wages_nominal` (BLS
mirror frozen at Jan 2025), `us`/`gb`/`ie`/`de.house_prices` (BIS mirror
frozen at Q1 2025), and ~24 `retail_sales`/`industrial_production`/
`consumer_confidence`/`equity_index`/`govt_bond_10y`/`unemployment_rate`/
`wages_nominal` series across `us`/`gb`/`de`/`ie`/`eu` (OECD mirror
systemically 74-135 days behind) -- see `indicators/providers/bls.py`,
`bis.py`, and `oecd.py`'s module docstrings, including BLS's
unregistered-tier quota constraints, the optional `BLS_API_KEY`, and
OECD's unusually strict rate limiting (config.yaml's `oecd` concurrency/
delay settings are deliberately the most conservative of any provider).
Every one of them
routes its HTTP call through `indicators/http.py`'s `get()` rather than
calling `httpx` directly -- see "Rate-limit safety" below. A `provider`
value in config with no matching module still degrades gracefully: `fetch.py`
logs a per-series `fetch_log` error (`"no provider module for
provider=..."`) rather than crashing the run.

---

## How `validate()` catches mistakes

`python -m indicators.config` (or `indicators.config.validate()`, called
automatically as the first step of both `fetch.py` and `db.py`) checks,
across every country in `countries.yaml`:

- `series/<iso>.yaml` exists at all.
- Every `core.yaml` key is present in that file (either mapped or marked
  `unavailable: true` + `unavailable_reason`).
- The resolved definition (country override merged over `core.yaml`) has
  every required field: `unit`, `frequency`, `transform_default`,
  `direction_good`.
- Every `news_topic_tags` value is a real theme from `config/taxonomy.yaml`.

It returns a list of problem strings (empty = valid) rather than raising --
callers decide whether to hard-fail. Per the brief's "only a
config-validation error should hard-fail" rule, a bad config should stop a
`fetch`/`db` run before it does anything, while a bad *provider response*
(a single series' fetch failing) should not.

Run it directly after any config edit:

```bash
python -m indicators.config
```

---

## Adding a country (Phase 5+ target: config-only)

1. Add an entry to `countries.yaml`.
2. Create `config/indicators/series/<iso>.yaml` with every `core.yaml` key
   mapped (or marked `unavailable`).
3. Add `<iso>` to `config.yaml`'s `indicators.active_countries` list.
4. `python -m indicators.config` to validate, then `python -m indicators.db`
   to sync the `series` table.

If step 2 needs new Python (a new provider module, a new transform), that's
a sign the Phase 1 abstraction was wrong somewhere -- per the brief, adding
a country from here on should be a config-editing task, not an archaeology
task.

## Adding an indicator (a new core key, or a country extra)

- **New core indicator** (applies to every country): add it to
  `core.yaml`, then add a matching entry (or `unavailable` marker) to
  *every* `series/<iso>.yaml` -- `validate()` will tell you if you miss one.
- **Country-specific extra** (Phase 4, not yet wired into `sync_series_from_config()`):
  add it under that country's `extras:` list in `series/<iso>.yaml`.

## Adding a provider

Implement `async def fetch(series_spec: dict) -> list[Observation]` in a new
`indicators/providers/<name>.py`, matching the `Provider` protocol in
`indicators/providers/__init__.py`. `series_spec` is a `series` table row
(dict-like); `indicators/fetch.py` dispatches to it by `series.provider`
(`importlib.import_module(f"indicators.providers.{provider_name}")`) --
no registry to update. Write tests against a recorded fixture (see
`tests/fixtures/dbnomics_gdpc1.json` and `tests/test_indicators_providers.py`)
rather than live network, matching the project's VCR-style convention.

---

## Release schedule grammar (`indicators/calendar.py`)

`series.release_schedule` was deliberately left undefined in
INDICATORS_SCHEMA_PROPOSAL.md ("brief §8: choose the specific schedule
yourself") -- `indicators/calendar.py` is the one place that defines it,
parses it, and computes forward dates from it. A rule string is:

```
<schedule>, <time>
```

`<schedule>` is one of:

| Shape | Example | Meaning |
|---|---|---|
| `daily` | `daily, 21:00 Europe/London` | Every day at the given local time. |
| `weekly <weekday>` | `weekly monday, 09:00 UTC` | Every week on that weekday. |
| `monthly <nth> <weekday>` | `monthly first friday, 08:30 America/New_York` | nth ∈ first/second/third/fourth/last (e.g. the US jobs report). |
| `monthly day <day>` | `monthly day 15, 09:00 UTC` | `<day>` is 1-31 or `last`; clamped to the month's actual last day (so `31` in February resolves to the 28th/29th). |
| `monthly lag <N>d` | `monthly lag 15d, 08:30 America/New_York` | N days after each month's end -- anchored on the series' own reference period, not the calendar month `after` falls in. |
| `quarterly lag <N>d` | `quarterly lag 45d, 08:30 America/New_York` | N days after each quarter's end (Mar/Jun/Sep/Dec 31). |
| `annual lag <N>d` | `annual lag 60d, 09:00 UTC` | N days after each year's end (Dec 31). |
| `annual month <M> day <D>` | `annual month 4 day 15, 06:00 UTC` | A fixed calendar day every year. |

`<time>` is always `HH:MM <IANA timezone>`, 24-hour, e.g. `07:00
Europe/London`. The timezone is not optional -- every rule needs one so
DST transitions resolve correctly (see the DST fixtures in
`tests/test_indicators_calendar.py`).

Two families, computed differently by `compute_next_release()`:

- **Fixed-calendar** rules (`daily`/`weekly`/`monthly <nth> <weekday>`/
  `monthly day`/`annual month day`) recur on their own clock, independent
  of any series data -- the US jobs report is always "first Friday of the
  month" regardless of what the prior report said.
- **Lag** rules (`monthly|quarterly|annual lag Nd`) are anchored on the
  series' own latest stored observation's `period_end` -- "N days after
  period end" needs to know *which* period end. With no observation yet
  (a brand-new series), it falls back to guessing the period containing
  `after` hasn't been released yet.

`scheduled_at` is always stored in `releases` as a UTC ISO datetime;
`scheduled_tz` keeps the rule's own IANA zone alongside it so the UI can
convert back to the visitor's local time (brief §8).

`indicators.calendar.populate_releases(conn)` is called at the start of
every `indicators.fetch.fetch_all()` run. For every active series with a
`release_schedule`, it: (1) resolves any past `'scheduled'` release to
`'released'` (a new observation was fetched on or after the scheduled
time) or `'missed'` (nothing landed within `MISSED_GRACE`, 5 days) or
leaves it `'scheduled'` (still within grace); (2) ensures a future
`'scheduled'` row exists, computing one via `compute_next_release()` if
not. It's idempotent -- a series with a future `'scheduled'` row already
is left alone, never duplicated. Where a provider exposes real
release-date metadata (none of Phase 1's providers currently do), prefer
that over the rule string -- this module's job is to be the fallback, not
the only source of truth.

Run it standalone with `python -m indicators.calendar`.

---

## Release-aware fetching (`indicators/fetch.py`)

Incremental mode's due-for-refresh check (`_select_due_series()`) now
looks up each series' nearest still-scheduled `releases` row (via
`indicators.export._next_release_by_series()`) alongside its last
successful fetch:

- **No `release_schedule` configured** (still most series -- see the
  per-country note above): falls back to the plain frequency-only
  heuristic `_is_due_for_refresh()` (`FREQUENCY_REFRESH_INTERVAL`).
- **Inside the release window** (`RELEASE_WINDOW_BEFORE` = 3 days before,
  `RELEASE_WINDOW_AFTER` = 7 days after the scheduled release): polled
  aggressively, every `AGGRESSIVE_REFRESH_INTERVAL` (6 hours) -- the print
  may land early, on time, or a little late.
- **Well outside the window**: backed off to
  `FAR_FROM_RELEASE_REFRESH_INTERVAL`, which is deliberately much coarser
  per frequency (e.g. 30 days for a quarterly series vs. the plain
  heuristic's 21) -- this is what satisfies "a series whose next release
  is six weeks away must not be refetched every run" (brief §8).

`--backfill` is unaffected -- it ignores the due-for-refresh check
entirely, same as Phase 1.

### Rate-limit safety

`indicators/http.py`'s `get()`/`request()` wraps every provider's HTTP
call with exponential backoff + jitter on 429/5xx (honouring a numeric
`Retry-After` header when present), up to `max_retries` (4 by default, so
5 attempts total) before propagating an ordinary `HTTPStatusError` --
which `fetch.py`'s per-series `try/except` already turns into a
`fetch_log` error row, never an aborted run. Any other 4xx (a genuinely
bad request) is not retried.

On top of that, `fetch.py` runs one `asyncio.Semaphore` *per provider*
(not one shared across all of them), sized from `config.yaml`'s
`indicators.provider_concurrency` (falling back to
`DEFAULT_PROVIDER_CONCURRENCY` = 3 for an unlisted provider), plus a
polite minimum delay before each request to that provider
(`indicators.provider_delay_seconds`, `DEFAULT_PROVIDER_DELAY` = 0.3s). A
slow/strict provider (BoE, Stooq -- both have shown bot-detection
behaviour, see their module docstrings) backing off never stalls a fast
one (DBnomics) sharing the same run.

### Revisions (`observation_revisions`)

Phase 1 left a TODO here: every observation write was `INSERT OR IGNORE`,
so a refetch that returned a *changed* value for a `(series_id,
period_start)` already in the db (a preliminary print later revised) was
silently dropped. `fetch.py`'s `_store_observations()` now does the
right thing per row: a new `period_start` is inserted as before; an
existing one with an identical value (the common case -- most of a
series' history never changes) is left untouched; one with a genuinely
different value (beyond `REVISION_EPSILON`, a float-noise guard) is
`UPDATE`d in place *and* the `(old_value, new_value, observed_at)` triple
is appended to `observation_revisions`. `indicators.export.build_series_history()`
already surfaces that log per series (used by the series page's revision
history table).

### Never fails the build

Per brief §8's "never fail the build" rule -- unchanged from Phase 1, just
reconfirmed still true after the above: a single series' provider raising
(after `http.py` exhausts its retries) is caught in `fetch_series()` and
logged as a `fetch_log` `'error'` row; the run continues with every other
series. `indicators.build`/`indicators.export` render whatever is in the
db regardless -- a series with no fresh data just serves its last-known
value and is flagged `stale` (`indicators.export.is_stale()`). Only
`indicators.config.validate()` failing hard-fails `build.py`.

---

## Running things

```bash
python -m indicators.config     # validate config, exit non-zero on problems
python -m indicators.db         # init schema + sync `series` table from config
python -m indicators.calendar   # reconcile past releases + schedule the next one per series
python -m indicators.fetch      # incremental fetch (skips series not yet "due", see below)
python -m indicators.fetch --backfill   # full historical pull, every active series unconditionally
python -m indicators.build      # render indicators/ HTML
python -m indicators.export     # write manifest.json + per-series JSON/CSV
```

**Incremental vs. `--backfill`:** most providers here always return a
series' full history regardless of mode -- there's no since-date query
parameter to ask for "just what's new". The default incremental mode
instead skips a series that isn't due yet, per the release-aware check
described above (falling back to the plain frequency heuristic for a
series with no `release_schedule`). `--backfill` ignores that check
entirely -- use it for a first-time population of `indicators.db`, or to
force a refetch of everything. It's wired up as a `workflow_dispatch`-only
job (`.github/workflows/indicators-backfill.yml`), not on any schedule --
the scheduled incremental workflow itself (`indicators.yml`) is still a
Phase 3 TODO.

`indicators.fetch.main()` calls `indicators.calendar.populate_releases()`
itself at the start of every run, so running `indicators.calendar`
standalone is only needed to inspect/refresh the `releases` table without
also fetching.

---

## Operations

### What runs when

`.github/workflows/indicators.yml` is the scheduled workflow -- a separate
failure domain from `pipeline.yml` (own concurrency group `indicators-db`,
own db file, own release tag). One job, two cron triggers, distinguished at
runtime via the `github.event.schedule` context value:

| Tier | Cron | What runs |
|---|---|---|
| **quick** | `8 2,5,8,11,14,17,20,23 * * *` (every 3h) | config validate -> db sync -> `indicators.fetch` (incremental) only |
| **full** | `38 3,15 * * *` (twice daily) | everything quick does, plus `indicators.transform`, `indicators.build`, `indicators.export`, and publishing the rendered site output |

Both tiers persist `data/indicators.db` (cache + release backup) every run,
so market data fetched by a "quick" run is never lost before the next
"full" run picks it up. The quick tier skips rebuilding the site because
`indicators.fetch`'s own due-for-refresh check (see "Release-aware
fetching" above) already limits what actually gets refetched on a
high-frequency schedule -- rebuilding+republishing HTML/JSON 8x/day for
data that mostly didn't change would be wasted work.

A `workflow_dispatch` (manual) run always takes the "full" path
(`github.event.schedule` is unset for manual runs).

**Site publishing:** `pipeline.yml` deploys to GitHub Pages; `indicators.yml`
never calls `deploy-pages` itself (to avoid the two workflows racing on the
same Pages environment). Instead, `indicators.yml`'s "full" tier caches its
rendered `site/indicators/` output under a uniquely-keyed `actions/cache`
entry (`indicators-site-<run_id>`), and `pipeline.yml` restores the newest
such entry into `site/indicators/` right after `python -m pipeline.build`
(which clears everything under `site/` except `archive/`/`story/`) and
before it runs Pagefind / uploads the Pages artifact. See the comments on
both workflows' relevant steps for the full reasoning. Net effect: the
published indicators section is only ever as stale as the last successful
`indicators.yml` "full" run, independent of when `pipeline.yml` last ran.

### Triggering a manual backfill

`.github/workflows/indicators-backfill.yml` (`workflow_dispatch` only) runs
`python -m indicators.fetch --backfill` -- a full historical pull for every
active series, ignoring the due-for-refresh check. Use it to (re)populate
`indicators.db` from scratch, or to force-refetch series the incremental
check has been skipping. It does not persist the db to cache/Releases
itself; run (or wait for) `indicators.yml` afterwards to bank the result,
or run `python -m indicators.backup_verify --repo <owner>/<repo> --write-counts ...`
and the release-upload steps manually if you need it banked immediately.

An on-demand *incremental* run (not a full backfill) is just
`indicators.yml`'s own `workflow_dispatch` trigger from the Actions tab --
it runs the full tier (fetch + transform + build + export + publish).

### Restoring `indicators.db` from a Release asset

The durable copy of record lives on the `indicators-db-latest` GitHub
Release (separate from the news pipeline's `db-latest`), as gzip snapshots
named `indicators.db.<run_id>.gz` -- newest 3 kept, older ones pruned
best-effort. To restore locally:

```bash
repo="<owner>/<repo>"
latest=$(gh api "repos/$repo/releases/tags/indicators-db-latest" --jq \
  '[.assets[] | select(.name | startswith("indicators.db."))] | sort_by(.created_at) | last | .name')
gh release download indicators-db-latest --repo "$repo" --pattern "$latest" --output data/indicators.db.gz
gunzip data/indicators.db.gz
python -m indicators.backup_verify --repo "$repo"   # confirms integrity_check + row counts
```

`.github/workflows/backup-restore-test.yml`'s `indicators-restore-test` job
runs this same check monthly on a fresh runner (read-only against the
release -- it never uploads, deletes, or overwrites a snapshot asset) to
prove the backup is actually restorable, not just uploaded.

### What to check first when a run fails

1. **`indicators.yml` failed at "Validate indicators config"** -- the only
   step allowed to hard-fail the whole run. Run `python -m indicators.config`
   locally; it prints every problem string (missing series entry, unknown
   `news_topic_tags`, etc.). Fix the YAML and re-run; nothing downstream
   ran, so there's no partial state to clean up.
2. **A specific series shows stale/missing data on the site, but the run
   went green** -- this is by design, not a failure: a provider error or an
   empty response for one series is logged to `fetch_log` (`status='error'`
   or `'stale'`) and the run continues (INDICATORS_BRIEF.md §8 "never fails
   the build"). Query `fetch_log` for that `series_id`, newest first, for
   the actual provider error.
3. **"Verify indicators db integrity before backup" failed** -- either
   `PRAGMA integrity_check` found real corruption, or a table's row count
   dropped more than `backup.max_row_count_drop_fraction` (config.yaml,
   shared with the news pipeline's equivalent check) vs. the previous
   snapshot. A GitHub Issue labelled `indicators-backup-integrity-failure`
   is filed automatically -- the backup step is skipped, so no bad snapshot
   gets banked on top of a good one.
4. **The indicators nav section is missing or visibly stale on the live
   site** -- check whether `indicators.yml`'s most recent "full" tier run
   actually reached its "Cache rendered indicators site output" step (a
   failure anywhere earlier in that run skips it). `pipeline.yml`'s next
   run will pick up the most recent successful one regardless of when its
   own run happens -- this is expected eventual consistency, not a bug,
   per the ordering note in both workflows' comments.
5. **Cache miss on `indicators-db-` and no releases exist yet** -- only
   expected on the very first run ever (or if the `indicators-db-latest`
   release was deleted). `indicators.fetch` starts from an empty db in this
   case; run `indicators-backfill.yml` to populate it in one shot rather
   than waiting for incremental due-checks to fill in history over time.

---

## Tests

| File | Covers |
|---|---|
| `tests/test_indicators_config.py` | `validate()` |
| `tests/test_indicators_db.py` | schema, `sync_series_from_config()` upsert behaviour |
| `tests/test_indicators_calendar.py` | `parse_rule()`/`compute_next_release()` against hand-checked fixtures (month-boundary clamping, DST spring-forward/autumn-back, lag-rule anchoring) + `populate_releases()`'s reconcile/idempotency/missed-vs-released behaviour |
| `tests/test_indicators_http.py` | exponential backoff + jitter on 429/5xx against a mocked transport, immediate propagation of a non-retryable 4xx |
| `tests/test_indicators_providers*.py` | each provider module against a recorded fixture |
| `tests/test_indicators_fx.py` | ECB/Stooq FX parsing + period-matched `convert()` |
| `tests/test_indicators_transform.py` | YoY/MoM/QoQ/annualised + derived-series formulas |
| `tests/test_indicators_fetch.py` | incremental due-for-refresh selection (both the plain and release-aware checks) vs. `--backfill`, `observation_revisions` logging on a changed refetch, and that one series' provider raising never aborts the others |
| `tests/test_indicators_build.py` | end-to-end smoke test: real 5-country config -> `db.init_db()`/`sync_series_from_config()` -> `fetch.fetch_all()` (mocked DBnomics, no live network) -> `transform.compute_derived()`, asserting every active dbnomics-backed core series ends up with a valid reference period |

None of these hit live network -- provider calls are mocked via
`httpx.MockTransport`, matching the existing project convention.
