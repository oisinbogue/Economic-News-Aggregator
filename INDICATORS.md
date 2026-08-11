# Indicators subsystem

Config-schema and operations reference for `indicators/` and
`config/indicators/`. See [`INDICATORS_SCHEMA_PROPOSAL.md`](INDICATORS_SCHEMA_PROPOSAL.md)
for the original design rationale and the full `indicators.db` schema; this
document is the day-to-day "how do I add a country / indicator / provider"
guide.

**Status:** Phase 1 (foundation) only -- config schema, `indicators.db`
schema, the DBnomics provider, transforms, FX, and tests. Five countries:
US, UK, Ireland, EU (euro area), Germany. No site output yet (Phase 2), no
scheduled fetch or release calendar yet (Phase 3), no country extras yet
(Phase 4), no countries beyond the five above (Phase 5+). The indicators
subsystem is a separate failure domain from the news pipeline -- separate
package, separate db file (`data/indicators.db`), separate GitHub Actions
workflows. Nothing here touches `pipeline/` or the site templates.

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
  # ...one entry per core.yaml key, every one of them, no exceptions...
extras: []                              # country-specific extras, populated Phase 4
```

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

**Providers implemented so far:** `dbnomics` only
(`indicators/providers/dbnomics.py`). Config already references `ecb`,
`eurostat`, `ons`, `boe`, and `stooq` for a handful of gb/ie/eu/de series
where DBnomics coverage is thin (brief §4) -- those provider modules don't
exist yet, so `indicators/fetch.py` logs a graceful per-series
`fetch_log` error for them (`"no provider module for provider=..."`) rather
than crashing the run. Implementing those modules is future work, not part
of Phase 1.

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

## Running things

```bash
python -m indicators.config     # validate config, exit non-zero on problems
python -m indicators.db         # init schema + sync `series` table from config
python -m indicators.fetch      # incremental fetch (skips series not yet "due", see below)
python -m indicators.fetch --backfill   # full historical pull, every active series unconditionally
```

**Incremental vs. `--backfill`:** DBnomics always returns a series' full
history regardless of mode -- there's no since-date query parameter to ask
for "just what's new". The default incremental mode instead skips a series
if it already has a successful fetch within roughly one release cycle
(`indicators/fetch.py`'s `FREQUENCY_REFRESH_INTERVAL`), so a normal run
doesn't re-pull hundreds of series' full history when almost none of them
could plausibly have changed. `--backfill` ignores that check entirely --
use it for a first-time population of `indicators.db`, or to force a
refetch of everything. It's wired up as a `workflow_dispatch`-only job
(`.github/workflows/indicators-backfill.yml`), not on any schedule -- the
scheduled incremental run is a Phase 3 deliverable.

---

## Tests

| File | Covers |
|---|---|
| `tests/test_indicators_config.py` | `validate()` |
| `tests/test_indicators_db.py` | schema, `sync_series_from_config()` upsert behaviour |
| `tests/test_indicators_providers.py` | `providers/dbnomics.py` against a recorded fixture |
| `tests/test_indicators_fx.py` | ECB/Stooq FX parsing + period-matched `convert()` |
| `tests/test_indicators_transform.py` | YoY/MoM/QoQ/annualised + derived-series formulas |
| `tests/test_indicators_fetch.py` | incremental due-for-refresh selection vs. `--backfill` |
| `tests/test_indicators_build.py` | end-to-end smoke test: real 5-country config -> `db.init_db()`/`sync_series_from_config()` -> `fetch.fetch_all()` (mocked DBnomics, no live network) -> `transform.compute_derived()`, asserting every active dbnomics-backed core series ends up with a valid reference period |

None of these hit live network -- provider calls are mocked via
`httpx.MockTransport`, matching the existing project convention.
