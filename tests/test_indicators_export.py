"""Tests for indicators/export.py -- the manifest the grid loads and the
per-series JSON/CSV the series page links to.

Everything here runs against a throwaway sqlite db built by the fixtures
below (indicators.db's real SCHEMA, synthetic rows), never the repo's
data/indicators.db, so the assertions don't depend on which providers
happened to answer on the last fetch run.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

import indicators.db as db_mod
import indicators.export as export_mod

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

SERIES_COLUMNS = (
    "series_id", "country", "indicator_key", "title", "topic", "tier", "unit",
    "frequency", "transform_default", "direction_good", "provider",
    "provider_series_id", "source_agency", "source_url", "licence",
)


def _insert_series(conn, series_id: str, *, frequency: str = "M", **overrides) -> None:
    country, indicator_key = series_id.split(".", 1)
    values = {
        "series_id": series_id,
        "country": country,
        "indicator_key": indicator_key,
        "title": f"{country.upper()} {indicator_key}",
        "topic": "prices",
        "tier": "core",
        "unit": "percent",
        "frequency": frequency,
        "transform_default": "yoy",
        "direction_good": "neutral",
        "provider": "dbnomics",
        "provider_series_id": "TEST/SERIES",
        "source_agency": "Test Agency",
        "source_url": "https://example.invalid/series",
        "licence": "public-domain",
    }
    values.update(overrides)
    columns = list(values)
    conn.execute(
        f"INSERT INTO series ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        [values[c] for c in columns],
    )


def _insert_monthly_observations(conn, series_id: str, values: list[float], last_month_start: str) -> None:
    """Inserts `values` as consecutive monthly observations ending at
    `last_month_start`, oldest first. period_end is the last day of each
    month, so the exported reference period is a real span, not a point.
    """
    year, month, _ = (int(p) for p in last_month_start.split("-"))
    rows = []
    for offset, value in enumerate(reversed(values)):
        total = year * 12 + (month - 1) - offset
        y, m = divmod(total, 12)
        start = f"{y:04d}-{m + 1:02d}-01"
        end_total = total + 1
        ey, em = divmod(end_total, 12)
        end = (datetime(ey, em + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        rows.append((series_id, start, end, "M", value, None, 0, NOW.isoformat()))
    conn.executemany(
        "INSERT INTO observations (series_id, period_start, period_end, frequency, value, "
        "value_usd, is_preliminary, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A fresh indicators db at tmp_path with the real schema, plus a frozen
    `now` so staleness assertions don't drift with the wall clock.
    """
    monkeypatch.setattr(db_mod, "get_db_path", lambda: tmp_path / "indicators.db")
    monkeypatch.setattr(export_mod, "_now", lambda: NOW)
    db_mod.init_db()
    with db_mod.get_connection() as connection:
        yield connection


@pytest.fixture
def populated(conn):
    """Three series: a healthy monthly one, a long daily one (to exercise
    sparkline downsampling), and a monthly one whose last observation is
    two years old (to exercise the stale rule).
    """
    _insert_series(conn, "ie.cpi_headline")
    _insert_monthly_observations(conn, "ie.cpi_headline", [1.0, 2.0, 3.5, 2.75], "2026-07-01")

    _insert_series(conn, "us.policy_rate", frequency="D", topic="rates", transform_default="level")
    # Anchored to NOW so this series is current (not stale) -- 500 days
    # ending yesterday.
    daily_rows = []
    for i in range(500):
        day = (NOW - timedelta(days=500 - i)).strftime("%Y-%m-%d")
        daily_rows.append(("us.policy_rate", day, day, "D", 4.0 + i * 0.001, None, 0, NOW.isoformat()))
    conn.executemany(
        "INSERT INTO observations (series_id, period_start, period_end, frequency, value, "
        "value_usd, is_preliminary, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        daily_rows,
    )

    _insert_series(conn, "gb.retail_sales", topic="economy")
    _insert_monthly_observations(conn, "gb.retail_sales", [10.0, 11.0], "2024-06-01")
    return conn


# --------------------------------------------------------------------------
# manifest.json
# --------------------------------------------------------------------------

def test_manifest_is_well_formed_json(populated, tmp_path):
    export_mod.write_exports(populated, tmp_path / "site")
    manifest_path = export_mod.data_dir(tmp_path / "site") / "manifest.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["generated"] == NOW.isoformat()
    assert manifest["series_count"] == 3
    assert len(manifest["series"]) == 3

    required = {
        "series_id", "country", "indicator_key", "title", "topic", "tier", "unit",
        "direction_good", "latest_value", "period_start", "period_end",
        "change_vs_prior", "sparkline", "source_agency", "source_url",
        "is_derived", "next_release", "stale",
    }
    for entry in manifest["series"]:
        assert required <= set(entry), f"{entry['series_id']} missing {required - set(entry)}"


def test_manifest_entries_carry_both_ends_of_the_reference_period(populated, tmp_path):
    """A reference period is not a release date -- both period_start and
    period_end must survive into the manifest so a consumer can label an
    August print as August, whenever it was published.
    """
    export_mod.write_exports(populated, tmp_path / "site")
    manifest = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "manifest.json").read_text(encoding="utf-8")
    )

    for entry in manifest["series"]:
        assert entry["period_start"] is not None, f"{entry['series_id']}: null period_start"
        assert entry["period_end"] is not None, f"{entry['series_id']}: null period_end"
        assert entry["period_start"] <= entry["period_end"]

    cpi = next(e for e in manifest["series"] if e["series_id"] == "ie.cpi_headline")
    assert cpi["period_start"] == "2026-07-01"
    assert cpi["period_end"] == "2026-07-31"


def test_sparklines_are_capped_at_40_points(populated, tmp_path):
    export_mod.write_exports(populated, tmp_path / "site")
    manifest = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "manifest.json").read_text(encoding="utf-8")
    )

    for entry in manifest["series"]:
        assert len(entry["sparkline"]) <= export_mod.SPARKLINE_POINTS, (
            f"{entry['series_id']}: {len(entry['sparkline'])} sparkline points"
        )

    # The 500-point daily series is downsampled; the 4-point monthly one is not.
    daily = next(e for e in manifest["series"] if e["series_id"] == "us.policy_rate")
    assert len(daily["sparkline"]) == export_mod.SPARKLINE_POINTS
    assert daily["sparkline"][-1] == pytest.approx(daily["latest_value"])

    cpi = next(e for e in manifest["series"] if e["series_id"] == "ie.cpi_headline")
    assert cpi["sparkline"] == [1.0, 2.0, 3.5, 2.75]


def test_downsample_preserves_first_and_last_points():
    values = [float(i) for i in range(1000)]
    result = export_mod.downsample(values, target=40)
    assert len(result) == 40
    assert result[0] == 0.0
    assert result[-1] == 999.0
    assert result == sorted(result)  # order preserved, not shuffled


def test_change_vs_prior_uses_the_previous_observation(populated, tmp_path):
    export_mod.write_exports(populated, tmp_path / "site")
    manifest = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "manifest.json").read_text(encoding="utf-8")
    )
    cpi = next(e for e in manifest["series"] if e["series_id"] == "ie.cpi_headline")
    assert cpi["latest_value"] == pytest.approx(2.75)
    assert cpi["change_vs_prior"] == pytest.approx(2.75 - 3.5)
    assert cpi["prior_period_start"] == "2026-06-01"


def test_single_observation_series_has_no_change(conn, tmp_path):
    _insert_series(conn, "us.gdp_real", frequency="Q")
    _insert_monthly_observations(conn, "us.gdp_real", [2.0], "2026-07-01")
    export_mod.write_exports(conn, tmp_path / "site")
    manifest = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["series"][0]
    assert entry["change_vs_prior"] is None
    assert entry["prior_period_start"] is None


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------

def test_stale_flag_for_a_series_with_an_old_last_observation(populated, tmp_path):
    export_mod.write_exports(populated, tmp_path / "site")
    manifest = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "manifest.json").read_text(encoding="utf-8")
    )
    by_id = {e["series_id"]: e for e in manifest["series"]}

    # Last monthly observation ends 2024-06-30, ~2 years before NOW.
    assert by_id["gb.retail_sales"]["stale"] is True
    # Current monthly + current daily series are not.
    assert by_id["ie.cpi_headline"]["stale"] is False
    assert by_id["us.policy_rate"]["stale"] is False
    assert manifest["stale_count"] == 1


@pytest.mark.parametrize(
    "frequency, days_since_period_end, expected_stale",
    [
        # interval x 1.5: M is 31d -> 46.5d, Q is 92d -> 138d, A is 366d -> 549d,
        # D is 4d -> 6d
        ("M", 40, False),
        ("M", 60, True),
        ("Q", 100, False),
        ("Q", 200, True),
        ("A", 400, False),
        ("A", 600, True),
        ("D", 4, False),
        ("D", 7, True),
    ],
)
def test_stale_rule_is_one_and_a_half_expected_intervals(frequency, days_since_period_end, expected_stale):
    period_end = (NOW - timedelta(days=days_since_period_end)).strftime("%Y-%m-%d")
    assert export_mod.is_stale(period_end, frequency, now=NOW) is expected_stale


def test_series_with_no_observations_is_stale(conn, tmp_path):
    _insert_series(conn, "de.house_prices", frequency="Q")
    export_mod.write_exports(conn, tmp_path / "site")
    manifest = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["series"][0]
    assert entry["stale"] is True
    assert entry["latest_value"] is None
    assert entry["sparkline"] == []


def test_inactive_series_are_excluded_from_the_manifest(populated, tmp_path):
    populated.execute("UPDATE series SET active = 0 WHERE series_id = 'gb.retail_sales'")
    export_mod.write_exports(populated, tmp_path / "site")
    manifest = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "manifest.json").read_text(encoding="utf-8")
    )
    assert {e["series_id"] for e in manifest["series"]} == {"ie.cpi_headline", "us.policy_rate"}
    assert not (export_mod.data_dir(tmp_path / "site") / "gb.retail_sales.json").exists()


# --------------------------------------------------------------------------
# Per-series JSON + CSV
# --------------------------------------------------------------------------

def test_per_series_json_round_trips_observation_values(populated, tmp_path):
    export_mod.write_exports(populated, tmp_path / "site")
    data = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "ie.cpi_headline.json").read_text(encoding="utf-8")
    )

    assert data["series_id"] == "ie.cpi_headline"
    assert data["generated"] == NOW.isoformat()
    assert [o["value"] for o in data["observations"]] == [1.0, 2.0, 3.5, 2.75]
    assert [o["period_start"] for o in data["observations"]] == [
        "2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01",
    ]
    assert all(o["period_end"] for o in data["observations"])

    # Full history, not the manifest's downsampled view.
    daily = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "us.policy_rate.json").read_text(encoding="utf-8")
    )
    assert len(daily["observations"]) == 500


def test_per_series_csv_round_trips_observation_values(populated, tmp_path):
    export_mod.write_exports(populated, tmp_path / "site")
    csv_path = export_mod.data_dir(tmp_path / "site") / "ie.cpi_headline.csv"

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert [r["series_id"] for r in rows] == ["ie.cpi_headline"] * 4
    assert [float(r["value"]) for r in rows] == [1.0, 2.0, 3.5, 2.75]
    assert [r["period_start"] for r in rows] == [
        "2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01",
    ]
    # value_usd is null for a percent-unit series -- empty cell, not "None".
    assert {r["value_usd"] for r in rows} == {""}


def test_value_usd_survives_json_and_csv(conn, tmp_path):
    _insert_series(conn, "gb.wages_nominal", unit="currency", unit_currency="GBP")
    conn.execute(
        "INSERT INTO observations (series_id, period_start, period_end, frequency, value, "
        "value_usd, is_preliminary, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("gb.wages_nominal", "2026-06-01", "2026-06-30", "M", 3000.0, 3810.5, 0, NOW.isoformat()),
    )
    export_mod.write_exports(conn, tmp_path / "site")
    target = export_mod.data_dir(tmp_path / "site")

    data = json.loads((target / "gb.wages_nominal.json").read_text(encoding="utf-8"))
    assert data["observations"][0]["value_usd"] == pytest.approx(3810.5)

    with open(target / "gb.wages_nominal.csv", newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert float(row["value_usd"]) == pytest.approx(3810.5)


def test_revision_log_is_exported_with_the_series(conn, tmp_path):
    _insert_series(conn, "us.gdp_real", frequency="Q", revisable=1)
    _insert_monthly_observations(conn, "us.gdp_real", [2.0, 2.4], "2026-07-01")
    conn.execute(
        "INSERT INTO observation_revisions (series_id, period_start, old_value, new_value, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("us.gdp_real", "2026-07-01", 2.1, 2.4, NOW.isoformat()),
    )
    export_mod.write_exports(conn, tmp_path / "site")

    data = json.loads(
        (export_mod.data_dir(tmp_path / "site") / "us.gdp_real.json").read_text(encoding="utf-8")
    )
    assert data["revisions"] == [
        {
            "period_start": "2026-07-01",
            "old_value": 2.1,
            "new_value": 2.4,
            "observed_at": NOW.isoformat(),
        }
    ]


def test_exports_are_fully_regenerated_not_appended(populated, tmp_path):
    """A rerun must not leave behind a file for a series that's gone, and
    must not double up rows in the CSVs (pipeline/export.py makes the same
    guarantee for its own exports).
    """
    output_dir = tmp_path / "site"
    export_mod.write_exports(populated, output_dir)
    target = export_mod.data_dir(output_dir)
    assert (target / "gb.retail_sales.json").exists()

    populated.execute("DELETE FROM observations WHERE series_id = 'gb.retail_sales'")
    populated.execute("DELETE FROM series WHERE series_id = 'gb.retail_sales'")
    export_mod.write_exports(populated, output_dir)

    assert not (target / "gb.retail_sales.json").exists()
    assert not (target / "gb.retail_sales.csv").exists()

    with open(target / "ie.cpi_headline.csv", newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 4


def test_manifest_stays_small(populated, tmp_path):
    """The grid's initial payload budget. 3 fixture series won't approach
    the real ceiling -- this guards the per-entry cost (i.e. that nobody
    puts full history back into the manifest), which is what actually
    scales.
    """
    stats = export_mod.write_exports(populated, tmp_path / "site")
    bytes_per_series = stats["manifest_bytes"] / stats["series"]
    assert bytes_per_series < 2048, f"{bytes_per_series:.0f} bytes/series in the manifest"
