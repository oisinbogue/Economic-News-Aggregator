"""Machine-readable exports for the indicators subsystem.

Mirrors pipeline/export.py's conventions exactly: everything is written
under the same `site.output_dir` the HTML is rendered into (so one GitHub
Pages deploy publishes both), every file is fully regenerated from
data/indicators.db on each run rather than appended to, and every JSON
file carries a `generated` ISO-8601 UTC timestamp header.

Three output shapes under `<output_dir>/indicators/data/`:

  manifest.json      The lightweight index the indicator grid loads on
                     first paint -- one entry per active series, with
                     just enough to render a card (latest value, change
                     vs. prior period, a downsampled sparkline, source
                     attribution, staleness). Deliberately does NOT carry
                     full history: that's what the per-series files are
                     for, and the brief wants the initial payload well
                     under a few hundred KB.
  <series_id>.json   Full history for one series -- every observation
                     (value + value_usd where present) plus that series'
                     revision log from `observation_revisions`. Loaded
                     lazily by the series page, never by the grid.
  <series_id>.csv    The same history as CSV, for the series page's
                     download link.

Reference periods are exported as *both* period_start and period_end,
never collapsed to a single date: a reference period is not a release
date, and an August CPI print published in September must not be
displayed as September data. Every consumer of these files gets the full
period so it can label it correctly.

Usage: python -m indicators.export  (run after `python -m indicators.fetch`;
creates its own output directory, so it does not require indicators.build
to have run first)
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from indicators.config import get_config, resolve_path
from indicators.db import get_connection, init_db
from indicators.transform import Point, apply_transform

# Target length of a manifest sparkline. The grid draws these a few dozen
# pixels wide, so more points than this is invisible detail paid for in
# payload size -- a daily series with 20 years of history is ~5,000 points,
# which alone would blow the manifest past the size budget.
SPARKLINE_POINTS = 40

# Nominal length of one reference period per frequency, used only for the
# staleness rule below. Approximate by construction (a "month" is not 30
# days) -- the 1.5x multiplier absorbs the slack, and nothing else depends
# on these numbers.
#
# "D" is 4 days rather than 1: a literal daily interval makes every
# market-data series (policy rates, bond yields, FX) flag stale across any
# normal weekend, since 1 day x 1.5 = 1.5 days and Friday-to-Monday alone is
# 3. 4 days x 1.5 = 6, which absorbs a weekend plus a one-day holiday
# without masking a series that's actually stopped updating.
FREQUENCY_INTERVAL: dict[str, timedelta] = {
    "D": timedelta(days=4),
    "W": timedelta(days=7),
    "M": timedelta(days=31),
    "Q": timedelta(days=92),
    "A": timedelta(days=366),
}

# How far past one nominal interval a series may go before it's flagged
# stale. 1.5x rather than 1.0x because a publication lag is normal: monthly
# CPI for August lands in mid-September, so a "monthly" series is routinely
# ~45 days past its last reference period start while perfectly healthy.
STALE_INTERVAL_MULTIPLIER = 1.5

CSV_FIELDS = [
    "series_id", "period_start", "period_end", "frequency",
    "value", "value_usd", "is_preliminary", "fetched_at",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generated_header() -> str:
    return _now().isoformat()


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------

def expected_interval(frequency: str) -> timedelta:
    """Nominal length of one reference period for `frequency`.

    Falls back to the monthly interval for an unrecognised frequency rather
    than raising -- an unknown frequency should not be able to abort an
    export run, and monthly is the modal case across the core 16.
    """
    return FREQUENCY_INTERVAL.get(frequency, FREQUENCY_INTERVAL["M"])


def is_stale(last_period_end: str | None, frequency: str, now: datetime | None = None) -> bool:
    """Whether a series counts as stale, per the brief's rule:
    `now - last_observation > expected_interval x 1.5`.

    Measured from the last observation's *period_end* (the last date the
    series actually covers), not period_start -- for a quarterly series
    those differ by nearly a whole interval, and measuring from the start
    would flag a perfectly current series as stale the moment its period
    closed.

    A series with no observations at all is stale by definition.
    """
    if not last_period_end:
        return True
    now = now or _now()
    try:
        end = datetime.fromisoformat(last_period_end).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - end) > expected_interval(frequency) * STALE_INTERVAL_MULTIPLIER


# --------------------------------------------------------------------------
# Sparkline downsampling
# --------------------------------------------------------------------------

def _downsample_indices(n: int, target: int) -> list[int]:
    """Picks at most `target` indices spread evenly across `range(n)`.

    Picks by index across the whole span (rather than taking the last N)
    so a downsampled series keeps the shape of the full history. Spacing
    runs from index 0 to index n-1 inclusive, so both the first and the
    last point always survive -- the line has to end on the value the
    card displays, or the chart and the headline number disagree.
    """
    if n <= target or target < 2:
        return list(range(n))[:target] if target < 2 else list(range(n))
    return sorted({round(i * (n - 1) / (target - 1)) for i in range(target)})


def downsample(values: list[float], target: int = SPARKLINE_POINTS) -> list[float]:
    """Reduces a value series to at most `target` evenly-spaced points."""
    return [values[i] for i in _downsample_indices(len(values), target)]


def downsample_points(observations: list[dict], target: int = SPARKLINE_POINTS) -> list[dict]:
    """Like `downsample`, but keeps each surviving point's `period_start`
    alongside its value -- needed to label a chart's x-axis, which a bare
    list of values cannot do.
    """
    indices = _downsample_indices(len(observations), target)
    return [
        {"period_start": observations[i]["period_start"], "value": observations[i]["value"]}
        for i in indices
    ]


# --------------------------------------------------------------------------
# DB reads
# --------------------------------------------------------------------------

def _active_series(conn) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM series WHERE active = 1 ORDER BY country, indicator_key"
        )
    ]


def _observations_by_series(conn) -> dict[str, list[dict]]:
    """Every observation for every series, oldest first, grouped by series_id.

    One query for the whole table rather than one per series: at Phase-1
    scale (80 series x a few thousand points) this is a small read, and it
    keeps the export a single pass over the db the way pipeline/export.py's
    CSV writer is.
    """
    grouped: dict[str, list[dict]] = {}
    for row in conn.execute(
        """
        SELECT series_id, period_start, period_end, frequency, value, value_usd,
               is_preliminary, fetched_at
        FROM observations
        ORDER BY series_id, period_start
        """
    ):
        grouped.setdefault(row["series_id"], []).append(dict(row))
    return grouped


def _revisions_by_series(conn) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in conn.execute(
        """
        SELECT series_id, period_start, old_value, new_value, observed_at
        FROM observation_revisions
        ORDER BY series_id, period_start, observed_at
        """
    ):
        grouped.setdefault(row["series_id"], []).append(dict(row))
    return grouped


def _next_release_by_series(conn) -> dict[str, str]:
    """Earliest still-scheduled release per series, or nothing if the
    `releases` table is unpopulated (it is until Phase 3 adds calendar.py
    -- the column is exported as null rather than omitted so the manifest
    shape doesn't change when that lands).
    """
    now = _now().isoformat()
    return {
        row["series_id"]: row["scheduled_at"]
        for row in conn.execute(
            """
            SELECT series_id, MIN(scheduled_at) AS scheduled_at
            FROM releases
            WHERE status = 'scheduled' AND scheduled_at >= ?
            GROUP BY series_id
            """,
            (now,),
        )
    }


def apply_series_transform(observations: list[dict], transform_default: str, frequency: str) -> list[dict]:
    """Turns a series' raw fetched levels into the "as displayed" series
    named by its `transform_default` (INDICATORS_BRIEF.md §5: "what the
    card shows") -- the same yoy/mom/qoq/annualised/level transform
    `compute_derived()` applies to a derived series' components. Every
    consumer of `observations` (manifest cards, the series JSON/CSV, the
    series page's chart and table) must read this transformed series, not
    the raw levels `observations` stores, or a `yoy` series shows its
    underlying index/level number instead of a percent change.

    A `yoy`/`mom`/`qoq` transform has no matching prior period for a
    series' earliest points, so the result can be shorter than the input
    -- `period_end`/`fetched_at`/etc. are carried over from the matching
    raw row by period_start. `value_usd` is dropped for anything but
    `level`: a currency conversion of a percent change is meaningless.
    """
    if not observations:
        return []
    points = [Point(date.fromisoformat(o["period_start"]), o["value"]) for o in observations]
    transformed = apply_transform(points, transform_default, frequency)
    by_period = {o["period_start"]: o for o in observations}
    result = []
    for p in transformed:
        period_start = p.period_start.isoformat()
        source = by_period[period_start]
        result.append({
            **source,
            "value": p.value,
            "value_usd": source["value_usd"] if transform_default == "level" else None,
        })
    return result


# --------------------------------------------------------------------------
# manifest.json
# --------------------------------------------------------------------------

def _change_vs_prior(observations: list[dict]) -> float | None:
    """Absolute change between the latest observation and the one before it.

    Absolute (latest - prior), not percent: these values are already mostly
    percentages (a CPI series stored as YoY %, a policy rate level), and a
    "percent change in a percent" is the classic way to turn 2.0% -> 2.5%
    into a misleading "+25%". Consumers that want a percentage of a level
    series can compute it from the full history file.
    """
    if len(observations) < 2:
        return None
    return observations[-1]["value"] - observations[-2]["value"]


def build_manifest_entry(
    series: dict,
    observations: list[dict],
    next_release: str | None,
    now: datetime,
) -> dict:
    latest = observations[-1] if observations else None
    return {
        "series_id": series["series_id"],
        "country": series["country"],
        "indicator_key": series["indicator_key"],
        "title": series["title"],
        "topic": series["topic"],
        "tier": series["tier"],
        "unit": series["unit"],
        "unit_currency": series["unit_currency"],
        "scale": series["scale"],
        "frequency": series["frequency"],
        "transform_default": series["transform_default"],
        "direction_good": series["direction_good"],
        # Needed to colour a `direction_good = neutral` series (inflation)
        # relative to its target band rather than the sign of its change --
        # see INDICATORS_BRIEF.md §9 "Colour semantics".
        "target_value": series["target_value"],
        "target_band_low": series["target_band_low"],
        "target_band_high": series["target_band_high"],
        "latest_value": latest["value"] if latest else None,
        # Both ends of the reference period, always -- see this module's
        # docstring on why this is never collapsed to one date.
        "period_start": latest["period_start"] if latest else None,
        "period_end": latest["period_end"] if latest else None,
        "change_vs_prior": _change_vs_prior(observations),
        "prior_period_start": observations[-2]["period_start"] if len(observations) >= 2 else None,
        "sparkline": downsample([o["value"] for o in observations]),
        "observation_count": len(observations),
        "source_agency": series["source_agency"],
        "source_url": series["source_url"],
        "is_derived": bool(series["is_derived"]),
        "derivation": series["derivation"],
        "notes": series["notes"],
        "next_release": next_release,
        "stale": is_stale(latest["period_end"] if latest else None, series["frequency"], now),
    }


def _displayed_observations_by_series(conn, series_rows: list[dict]) -> dict[str, list[dict]]:
    """`_observations_by_series()` with each series' `transform_default`
    applied, i.e. the "as displayed" values rather than the raw fetched
    levels.

    Every export consumer must go through this rather than reading
    `_observations_by_series()` directly -- see `apply_series_transform()`.
    Skipping it is what made a `yoy` series like de.gdp_real render its raw
    chain-linked volume level (907,884) formatted as a percent.
    """
    raw = _observations_by_series(conn)
    return {
        series["series_id"]: apply_series_transform(
            raw.get(series["series_id"], []),
            series["transform_default"],
            series["frequency"],
        )
        for series in series_rows
    }


def build_manifest(conn) -> dict:
    now = _now()
    series_rows = _active_series(conn)
    observations = _displayed_observations_by_series(conn, series_rows)
    next_releases = _next_release_by_series(conn)
    entries = [
        build_manifest_entry(
            series,
            observations.get(series["series_id"], []),
            next_releases.get(series["series_id"]),
            now,
        )
        for series in series_rows
    ]
    return {
        "generated": now.isoformat(),
        "sparkline_points": SPARKLINE_POINTS,
        "series_count": len(entries),
        "stale_count": sum(1 for e in entries if e["stale"]),
        "series": entries,
    }


# --------------------------------------------------------------------------
# Per-series history (JSON + CSV)
# --------------------------------------------------------------------------

def build_series_history(series: dict, observations: list[dict], revisions: list[dict]) -> dict:
    return {
        "generated": _generated_header(),
        "series_id": series["series_id"],
        "country": series["country"],
        "indicator_key": series["indicator_key"],
        "title": series["title"],
        "topic": series["topic"],
        "unit": series["unit"],
        "unit_currency": series["unit_currency"],
        "scale": series["scale"],
        "frequency": series["frequency"],
        "transform_default": series["transform_default"],
        "seasonal_adjustment": series["seasonal_adjustment"],
        "direction_good": series["direction_good"],
        "target_value": series["target_value"],
        "target_band_low": series["target_band_low"],
        "target_band_high": series["target_band_high"],
        "source_agency": series["source_agency"],
        "source_url": series["source_url"],
        "licence": series["licence"],
        "provider": series["provider"],
        "is_derived": bool(series["is_derived"]),
        "revisable": bool(series["revisable"]),
        "notes": series["notes"],
        "observations": [
            {
                "period_start": o["period_start"],
                "period_end": o["period_end"],
                "value": o["value"],
                "value_usd": o["value_usd"],
                "is_preliminary": bool(o["is_preliminary"]),
                "fetched_at": o["fetched_at"],
            }
            for o in observations
        ],
        "revisions": [
            {
                "period_start": r["period_start"],
                "old_value": r["old_value"],
                "new_value": r["new_value"],
                "observed_at": r["observed_at"],
            }
            for r in revisions
        ],
    }


def _write_series_csv(path: Path, series_id: str, observations: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(
            {
                "series_id": series_id,
                "period_start": o["period_start"],
                "period_end": o["period_end"],
                "frequency": o["frequency"],
                "value": o["value"],
                "value_usd": "" if o["value_usd"] is None else o["value_usd"],
                "is_preliminary": int(bool(o["is_preliminary"])),
                "fetched_at": o["fetched_at"],
            }
            for o in observations
        )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def data_dir(output_dir: Path) -> Path:
    return output_dir / "indicators" / "data"


def write_exports(conn, output_dir: Path) -> dict:
    """Writes manifest.json plus one JSON + one CSV per active series.

    The data directory is cleared of previously-exported files first, so a
    series that leaves the config (or is deactivated) doesn't linger on the
    published site as a file nothing links to any more -- same
    "fully regenerated from the db every run" guarantee pipeline/export.py
    makes for its CSVs.
    """
    target = data_dir(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    for stale_file in target.glob("*.json"):
        stale_file.unlink()
    for stale_file in target.glob("*.csv"):
        stale_file.unlink()

    series_rows = _active_series(conn)
    observations_by_series = _displayed_observations_by_series(conn, series_rows)
    revisions_by_series = _revisions_by_series(conn)

    for series in series_rows:
        series_id = series["series_id"]
        observations = observations_by_series.get(series_id, [])
        history = build_series_history(series, observations, revisions_by_series.get(series_id, []))
        (target / f"{series_id}.json").write_text(
            json.dumps(history, ensure_ascii=False), encoding="utf-8"
        )
        _write_series_csv(target / f"{series_id}.csv", series_id, observations)

    manifest = build_manifest(conn)
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    return {
        "series": len(series_rows),
        "observations": sum(len(observations_by_series.get(s["series_id"], [])) for s in series_rows),
        "stale": manifest["stale_count"],
        "manifest_bytes": manifest_path.stat().st_size,
    }


def main() -> dict:
    init_db()
    cfg = get_config()
    output_dir = resolve_path(cfg["site"].get("output_dir", "site"))

    print("Writing indicator exports (manifest.json, per-series JSON + CSV)...")
    with get_connection() as conn:
        stats = write_exports(conn, output_dir)

    print(
        f"Done: {stats['series']} series ({stats['stale']} stale), "
        f"{stats['observations']} observation(s), "
        f"manifest.json {stats['manifest_bytes'] / 1024:.1f} KB."
    )
    return stats


if __name__ == "__main__":
    main()
