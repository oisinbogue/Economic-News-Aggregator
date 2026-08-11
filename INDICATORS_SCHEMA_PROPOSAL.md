# Indicators subsystem — schema proposal (for review)

Companion to `INDICATORS_BRIEF.md`. Scope: Phase 1 only (US, UK, Ireland, EU,
Germany; core 16 + derived series; DBnomics provider; no site/automation yet).
Nothing here is implemented — this is the schema to review before any
provider code is written, per the brief's own §13 instruction.

Everything below deliberately mirrors existing `pipeline/` conventions
(raw `sqlite3`, PyYAML `safe_load`, no ORM, no validation library) rather
than introducing new patterns, per `PROJECT_BRIEF.md`'s "don't re-litigate
architecture" rule.

---

## 1. Config layout

```
config/indicators/
  countries.yaml   # the 5 Phase-1 countries/blocs
  core.yaml        # the 16 core indicator definitions (country-agnostic)
  series/
    us.yaml        # per-country series -> provider mapping
    gb.yaml
    ie.yaml
    eu.yaml
    de.yaml
```

`countries.yaml`:

```yaml
# One entry per country/bloc. iso is the alpha-2 code used as the series_id
# prefix and the series/<iso>.yaml filename; EU is a bloc, not a country,
# but is treated identically everywhere else in this schema.
countries:
  us:
    iso: us
    name: United States
    currency: USD
    timezone: America/New_York
  gb:
    iso: gb
    name: United Kingdom
    currency: GBP
    timezone: Europe/London
  ie:
    iso: ie
    name: Ireland
    currency: EUR
    timezone: Europe/Dublin
  eu:
    iso: eu
    name: Euro Area
    currency: EUR
    timezone: Europe/Brussels
  de:
    iso: de
    name: Germany
    currency: EUR
    timezone: Europe/Berlin
```

`core.yaml` — the 16 country-agnostic definitions from brief §6.1
(key, topic, unit, default transform, direction_good, target band where
applicable). This file has **no provider info** — providers are per
country in `series/<iso>.yaml`, because the same indicator key is sourced
differently per country (e.g. `policy_rate` is a Fed funds rate via
DBnomics/FRED for `us`, an ECB deposit rate via DBnomics/ECB for `eu`).

```yaml
indicators:
  gdp_real:
    topic: economy
    unit: percent
    transform_default: yoy
    direction_good: up
    frequency: Q
  cpi_headline:
    topic: prices
    unit: percent
    transform_default: yoy
    direction_good: neutral
    frequency: M
    target_band: [1.0, 3.0]   # only present where a target exists (ECB/Fed ~2%)
  policy_rate:
    topic: rates
    unit: percent
    transform_default: level
    direction_good: neutral
    frequency: D
  # ... remaining 13, one block each
```

`series/us.yaml` — maps each `core.yaml` key (plus country extras, Phase 4)
to a provider chain. This is the file that fails config validation loudly
if a core indicator has no provider:

```yaml
country: us
series:
  gdp_real:
    provider: dbnomics
    provider_series_id: FRED/GDPC1
    fallback_providers: []
    source_agency: Bureau of Economic Analysis
    source_url: https://www.bea.gov/data/gdp
    licence: public-domain
    seasonal_adjustment: sa
    revisable: true
    news_topic_tags: [MACROECONOMICS, GLOBAL ECONOMY]
  cpi_headline:
    provider: dbnomics
    provider_series_id: FRED/CPIAUCSL
    fallback_providers: []
    source_agency: Bureau of Labor Statistics
    source_url: https://www.bls.gov/cpi/
    licence: public-domain
    seasonal_adjustment: sa
    revisable: false
    news_topic_tags: [MACROECONOMICS, INEQUALITY & LABOUR]
  # ... remaining 14 core keys, each explicit -- no silent gaps
extras: []   # populated in Phase 4
```

Notes:
- `news_topic_tags` values are drawn from the closed 8-theme set already
  defined in `config/taxonomy.yaml`'s `keywords:` top-level keys (per
  Explore findings) — reusing that vocabulary rather than inventing a new
  one, exactly as the brief's §5 intends for future cross-linking.
- A series present in `core.yaml` but **absent** from a country's
  `series/<iso>.yaml` is a config validation error (brief §11: "every
  core indicator exists for every active country or is explicitly marked
  unavailable"). Explicit unavailability is modeled as an `unavailable:
  true` + `unavailable_reason: "..."` entry rather than omission, so a
  missing key is unambiguously a bug, not a shrug.

## 2. `indicators/config.py`

Same shape as `pipeline/config.py`, but loading the indicators tree
instead of (not merged into) `config.yaml`, since these are structural
seed files like `feeds.yaml`/`taxonomy.yaml`, not runtime knobs:

```python
INDICATORS_CONFIG_DIR = REPO_ROOT / "config" / "indicators"

@lru_cache(maxsize=1)
def get_countries() -> dict: ...   # config/indicators/countries.yaml

@lru_cache(maxsize=1)
def get_core_indicators() -> dict: ...  # config/indicators/core.yaml

@lru_cache(maxsize=1)
def get_series(country_iso: str) -> dict: ...  # config/indicators/series/<iso>.yaml

def validate() -> list[str]:
    """Returns a list of problems; empty = valid. Called by both
    `python -m indicators.config` (manual check) and as the first step of
    fetch.py/build.py, which exit non-zero on any problem rather than
    proceeding with partial config -- matches the brief's "only a
    config-validation error should hard-fail" rule."""
```

Root `config.yaml` gets one new top-level section (runtime knobs only,
matching the `tag:`/`cluster:` pattern) plus one `paths:` addition:

```yaml
paths:
  indicators_db_file: data/indicators.db

indicators:
  active_countries: [us, gb, ie, eu, de]   # Phase-1 gate; Phase 5+ grows this list
  dbnomics_base_url: https://api.db.nomics.world/v22/
  fetch_concurrency: 5
  fetch_timeout_seconds: 20
```

## 3. `indicators/db.py` schema

Separate file (`data/indicators.db`), separate `get_connection()`/
`init_db()` pair, same `PRAGMA journal_mode=WAL` + `foreign_keys=ON` +
`row_factory=sqlite3.Row` setup as `pipeline/db.py`.

```sql
CREATE TABLE IF NOT EXISTS series (
  series_id             TEXT PRIMARY KEY,      -- e.g. 'ie.cpi_headline'
  country                TEXT NOT NULL,         -- iso from countries.yaml, or 'EU'
  indicator_key          TEXT NOT NULL,         -- matches core.yaml key or a country extra key
  title                  TEXT NOT NULL,
  topic                  TEXT NOT NULL CHECK (topic IN (
                             'economy','prices','labour','rates','housing',
                             'confidence','government','trade','energy','markets')),
  tier                   TEXT NOT NULL CHECK (tier IN ('core','extra')),
  unit                   TEXT NOT NULL CHECK (unit IN (
                             'percent','index','currency','ratio','count',
                             'percent_gdp','bps')),
  unit_currency          TEXT,                  -- ISO 4217, only when unit='currency'
  scale                  TEXT NOT NULL DEFAULT 'units' CHECK (scale IN (
                             'units','thousands','millions','billions','trillions')),
  frequency              TEXT NOT NULL CHECK (frequency IN ('D','W','M','Q','A')),
  transform_default      TEXT NOT NULL CHECK (transform_default IN (
                             'yoy','mom','qoq','level','annualised')),
  seasonal_adjustment    TEXT NOT NULL DEFAULT 'unknown' CHECK (seasonal_adjustment IN ('sa','nsa','unknown')),
  direction_good         TEXT NOT NULL CHECK (direction_good IN ('up','down','neutral')),
  target_value           REAL,
  target_band_low        REAL,
  target_band_high       REAL,
  provider               TEXT NOT NULL,         -- 'dbnomics' | 'ecb' | 'stooq' | ...
  provider_series_id     TEXT NOT NULL,
  fallback_providers     TEXT NOT NULL DEFAULT '[]',  -- JSON list, same convention as articles.topics being a flat column (not a join table) for a small closed vocabulary
  source_agency          TEXT NOT NULL,
  source_url             TEXT NOT NULL,
  licence                TEXT NOT NULL,
  release_schedule       TEXT,                  -- rule string, see calendar.py; nullable until Phase 3
  release_lag_days       INTEGER,
  is_derived             INTEGER NOT NULL DEFAULT 0 CHECK (is_derived IN (0,1)),
  derivation             TEXT,                  -- expression spec, required if is_derived=1
  revisable              INTEGER NOT NULL DEFAULT 0 CHECK (revisable IN (0,1)),
  notes                  TEXT,
  news_topic_tags        TEXT NOT NULL DEFAULT '[]',  -- JSON list, values from config/taxonomy.yaml's 8 themes
  comparable             INTEGER NOT NULL DEFAULT 0 CHECK (comparable IN (0,1)),  -- brief §6.4, unused until later phase
  active                 INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  unavailable_reason     TEXT
);

CREATE TABLE IF NOT EXISTS observations (
  series_id       TEXT NOT NULL REFERENCES series(series_id),
  period_start    TEXT NOT NULL,   -- ISO date, first day of the reference period
  period_end      TEXT NOT NULL,
  frequency       TEXT NOT NULL CHECK (frequency IN ('D','W','M','Q','A')),
  value           REAL NOT NULL,
  value_usd       REAL,            -- NULL for non-currency units; see fx.py
  is_preliminary  INTEGER NOT NULL DEFAULT 0 CHECK (is_preliminary IN (0,1)),
  fetched_at      TEXT NOT NULL,
  PRIMARY KEY (series_id, period_start)
);
CREATE INDEX IF NOT EXISTS idx_observations_series_period
  ON observations(series_id, period_start DESC);

CREATE TABLE IF NOT EXISTS observation_revisions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id     TEXT NOT NULL REFERENCES series(series_id),
  period_start  TEXT NOT NULL,
  old_value     REAL NOT NULL,
  new_value     REAL NOT NULL,
  observed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_series ON observation_revisions(series_id, period_start);

CREATE TABLE IF NOT EXISTS releases (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id     TEXT NOT NULL REFERENCES series(series_id),
  scheduled_at  TEXT NOT NULL,    -- UTC ISO datetime
  scheduled_tz  TEXT NOT NULL,    -- IANA tz the rule was computed in, for display conversion
  actual_at     TEXT,
  status        TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','released','missed'))
);
CREATE INDEX IF NOT EXISTS idx_releases_series_scheduled ON releases(series_id, scheduled_at);

CREATE TABLE IF NOT EXISTS fetch_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id    TEXT NOT NULL REFERENCES series(series_id),
  run_at       TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('ok','stale','error')),
  detail       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_series_run ON fetch_log(series_id, run_at DESC);

CREATE TABLE IF NOT EXISTS fx_rates (
  date   TEXT NOT NULL,
  base   TEXT NOT NULL,   -- ISO 4217, always 'EUR' in Phase 1 (ECB reference rates are EUR-based)
  quote  TEXT NOT NULL,
  rate   REAL NOT NULL,
  PRIMARY KEY (date, base, quote)
);
```

Design choices worth flagging for review:

- **`fallback_providers` / `news_topic_tags` as JSON-in-TEXT columns**,
  not join tables. Matches `articles.topics` being a flat TEXT column for
  a small closed vocabulary (per Explore findings) rather than
  normalizing into a many-to-many table — consistent with this repo's
  general preference for simplicity over relational purity at this scale
  (dozens of series, not millions).
- **`value_usd` stored at fetch time**, not computed on read, per brief
  §7's period-matched-rate requirement — avoids a join against
  `fx_rates` on every page render and makes "what rate was used" auditable
  from the row itself.
- **No `CHECK` on `country`** against `countries.yaml` — SQLite can't
  reference YAML, so this is enforced by `indicators/config.py`'s
  `validate()` at build time instead, same division of labour as the rest
  of the pipeline (DB enforces types/enums it can see; config.py enforces
  cross-file consistency).
- Migration convention (adding columns later) follows `pipeline/db.py`'s
  `ALTER TABLE` + `PRAGMA table_info` pattern exactly — no new tooling.

## 4. What's deliberately deferred to later phases

- `calendar.py` (release scheduling) — `releases` table exists now so
  Phase 3 doesn't need a schema change, but nothing populates it in Phase 1.
- Site templates / `indicators.js` / nav change — Phase 2.
- The GitHub Actions workflow and DB-backup-to-Releases wiring — Phase 3.
  (Mechanically this will extend `pipeline.yml` with a parallel
  verify+backup+prune block for `indicators.db`, reusing the same
  never-overwrite-in-place asset-naming trick, per the Explore findings above.)
- Country extras, comparison series — Phase 4/6.

## 5. Open questions before implementation starts

1. `core.yaml`'s `target_band` — only CPI and maybe policy-rate proxies
   have a real target. Confirm the brief wants this on the indicator
   definition (country-agnostic band) vs. per-country override (an ECB
   2% target differs subtly from a Fed 2% average-inflation target) —
   proposal above puts it in `core.yaml` for Phase 1 simplicity, with a
   per-country override slot left open in `series/<iso>.yaml` if needed later.
2. `release_schedule` rule-string grammar isn't defined yet (Phase 3
   concern) — flagging now only so the column exists in the right shape.
3. Should `indicators/config.py` live under the existing `lru_cache`
   single-load pattern (like `pipeline/config.py`), or do you want a
   `reload=True` escape hatch for tests? Existing pipeline has no such
   hatch and just re-imports in tests — proposing the same here for consistency.

If this shape looks right, next step is implementing `indicators/config.py`
+ `indicators/db.py` + the 5 YAML files + `tests/test_indicators_config.py`
(config validation tests per brief §11), still with no provider code yet.
