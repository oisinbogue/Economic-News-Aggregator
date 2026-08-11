"""SQLite schema and connection helper for the indicators subsystem.

Separate db file (data/indicators.db), separate get_connection()/init_db()
pair from pipeline/db.py -- the indicators subsystem is a separate failure
domain from the news pipeline (INDICATORS_BRIEF.md §2), so it doesn't share
a connection or schema with the articles/clusters db. See
INDICATORS_SCHEMA_PROPOSAL.md §3 for the design rationale behind each table.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from indicators.config import get_config, get_core_indicators, get_countries, get_series, resolve_path

SCHEMA = """
-- One row per tracked series (e.g. 'ie.cpi_headline'). Populated from
-- config/indicators/*.yaml by sync_series_from_config() -- this table is a
-- materialised view of the YAML config, not hand-edited.
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
"""


def get_db_path() -> Path:
    cfg = get_config()
    return resolve_path(cfg["paths"]["indicators_db_file"])


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Yields a sqlite3 connection with WAL mode + foreign keys enabled.

    Use as a context manager so the connection is always closed:
        with get_connection() as conn:
            conn.execute(...)
    """
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Creates all tables/indexes if they don't already exist. Safe to call every run."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)


def sync_series_from_config() -> int:
    """Upserts the `series` table from config/indicators/*.yaml.

    Only core (tier='core') indicators are synced -- country `extras` are a
    Phase 4 concern (INDICATORS_SCHEMA_PROPOSAL.md §4). An `unavailable: true`
    series entry is still inserted (active=0, unavailable_reason set) rather
    than skipped, so its absence is visible in the db, not silently missing --
    provider/source columns get empty-string placeholders since the schema
    requires them NOT NULL but an unavailable series genuinely has none.

    Re-running after a config edit updates existing rows in place (upsert on
    series_id) rather than duplicating them.

    Returns the number of series rows upserted.
    """
    countries = get_countries()
    core_indicators = get_core_indicators()

    rows = []
    for iso, country in countries.items():
        series = get_series(iso)
        for indicator_key, core_def in core_indicators.items():
            entry = series.get(indicator_key)
            if entry is None:
                continue

            resolved = {**core_def, **entry}
            unavailable = bool(entry.get("unavailable"))
            target_band = resolved.get("target_band") or [None, None]

            rows.append((
                f"{iso}.{indicator_key}",
                iso,
                indicator_key,
                f"{country['name']} {indicator_key.replace('_', ' ').title()}",
                resolved["topic"],
                "core",
                resolved["unit"],
                resolved.get("unit_currency"),
                resolved.get("scale", "units"),
                resolved["frequency"],
                resolved["transform_default"],
                resolved.get("seasonal_adjustment", "unknown"),
                resolved["direction_good"],
                resolved.get("target_value"),
                target_band[0],
                target_band[1],
                resolved.get("provider") or "none",
                resolved.get("provider_series_id") or "",
                json.dumps(resolved.get("fallback_providers", [])),
                resolved.get("source_agency") or "",
                resolved.get("source_url") or "",
                resolved.get("licence") or "",
                resolved.get("release_schedule"),
                resolved.get("release_lag_days"),
                int(bool(resolved.get("is_derived"))),
                resolved.get("derivation"),
                int(bool(resolved.get("revisable"))),
                resolved.get("notes"),
                json.dumps(resolved.get("news_topic_tags", [])),
                int(bool(resolved.get("comparable"))),
                0 if unavailable else 1,
                entry.get("unavailable_reason"),
            ))

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO series (
                series_id, country, indicator_key, title, topic, tier, unit,
                unit_currency, scale, frequency, transform_default,
                seasonal_adjustment, direction_good, target_value,
                target_band_low, target_band_high, provider, provider_series_id,
                fallback_providers, source_agency, source_url, licence,
                release_schedule, release_lag_days, is_derived, derivation,
                revisable, notes, news_topic_tags, comparable, active,
                unavailable_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(series_id) DO UPDATE SET
                country=excluded.country,
                indicator_key=excluded.indicator_key,
                title=excluded.title,
                topic=excluded.topic,
                tier=excluded.tier,
                unit=excluded.unit,
                unit_currency=excluded.unit_currency,
                scale=excluded.scale,
                frequency=excluded.frequency,
                transform_default=excluded.transform_default,
                seasonal_adjustment=excluded.seasonal_adjustment,
                direction_good=excluded.direction_good,
                target_value=excluded.target_value,
                target_band_low=excluded.target_band_low,
                target_band_high=excluded.target_band_high,
                provider=excluded.provider,
                provider_series_id=excluded.provider_series_id,
                fallback_providers=excluded.fallback_providers,
                source_agency=excluded.source_agency,
                source_url=excluded.source_url,
                licence=excluded.licence,
                release_schedule=excluded.release_schedule,
                release_lag_days=excluded.release_lag_days,
                is_derived=excluded.is_derived,
                derivation=excluded.derivation,
                revisable=excluded.revisable,
                notes=excluded.notes,
                news_topic_tags=excluded.news_topic_tags,
                comparable=excluded.comparable,
                active=excluded.active,
                unavailable_reason=excluded.unavailable_reason
            """,
            rows,
        )

    return len(rows)


if __name__ == "__main__":
    # `python -m indicators.db` initialises the db file and syncs `series`
    # from config -- handy for a first-time setup check without going
    # through the (not-yet-written) fetch pipeline.
    init_db()
    n = sync_series_from_config()
    with get_connection() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("series", "observations", "releases", "fetch_log", "fx_rates")
        }
    print(f"Initialised schema at {get_db_path()}")
    print(f"Synced {n} series rows from config")
    for table, count in counts.items():
        print(f"  {table}: {count} rows")
