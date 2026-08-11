"""Tests for indicators/transform.py: yoy/mom/qoq/annualised transforms and
the brief-§6.2 derived-series formulas + compute_derived() dispatcher.

Fixture values are hand-checked (see each test's inline comment), matching
the fixture conventions in tests/test_indicators_providers.py.
"""

from datetime import date

import pytest

import indicators.transform as transform
from indicators.transform import Point


def pt(iso_date, value):
    return Point(date.fromisoformat(iso_date), value)


# --------------------------------------------------------------------------
# yoy
# --------------------------------------------------------------------------

def test_yoy_monthly_compares_twelve_months_back():
    points = [pt("2023-01-01", 100.0), pt("2024-01-01", 110.0)]
    result = transform.yoy(points, "M")
    assert len(result) == 1
    assert result[0].period_start == date(2024, 1, 1)
    assert result[0].value == pytest.approx(10.0)


def test_yoy_quarterly_compares_four_periods_back_not_twelve():
    # Q1 2023 -> Q1 2024 is 4 quarters (12 months) back, not 12 rows back.
    points = [pt("2023-01-01", 200.0), pt("2023-04-01", 205.0), pt("2023-07-01", 210.0),
              pt("2023-10-01", 215.0), pt("2024-01-01", 220.0)]
    result = transform.yoy(points, "Q")
    matches = {p.period_start: p.value for p in result}
    assert matches[date(2024, 1, 1)] == pytest.approx((220.0 - 200.0) / 200.0 * 100.0)
    # Q4 2023 has no Q4 2022 to compare against -> not present.
    assert date(2023, 10, 1) not in matches


def test_yoy_annual_compares_one_period_back():
    points = [pt("2022-01-01", 50.0), pt("2023-01-01", 55.0), pt("2024-01-01", 60.5)]
    result = transform.yoy(points, "A")
    values = {p.period_start: p.value for p in result}
    assert values[date(2023, 1, 1)] == pytest.approx(10.0)
    assert values[date(2024, 1, 1)] == pytest.approx(10.0)


def test_yoy_skips_partial_year_history():
    # Series starts June 2023; by May 2024 there's still no full year of
    # prior data for Jan-May, only Jun 2023 onward has a match.
    points = [
        pt("2023-06-01", 100.0), pt("2023-07-01", 101.0), pt("2023-08-01", 102.0),
        pt("2024-01-01", 103.0), pt("2024-06-01", 110.0),
    ]
    result = transform.yoy(points, "M")
    matched_dates = {p.period_start for p in result}
    # 2024-01-01 has no 2023-01-01 counterpart in the data -> excluded.
    assert date(2024, 1, 1) not in matched_dates
    # 2024-06-01 does have a 2023-06-01 counterpart -> included.
    assert date(2024, 6, 1) in matched_dates


def test_yoy_daily_leap_day_falls_back_to_feb_28():
    # 2024 is a leap year; one year before 2024-02-29 has no calendar
    # equivalent, so the lookback must fall back to 2023-02-28.
    points = [pt("2023-02-28", 100.0), pt("2024-02-29", 105.0)]
    result = transform.yoy(points, "D")
    assert len(result) == 1
    assert result[0].period_start == date(2024, 2, 29)
    assert result[0].value == pytest.approx(5.0)


# --------------------------------------------------------------------------
# mom / qoq
# --------------------------------------------------------------------------

def test_mom_one_month_back():
    points = [pt("2024-01-01", 100.0), pt("2024-02-01", 102.0), pt("2024-03-01", 101.0)]
    result = transform.mom(points)
    values = {p.period_start: p.value for p in result}
    assert values[date(2024, 2, 1)] == pytest.approx(2.0)
    assert values[date(2024, 3, 1)] == pytest.approx((101.0 - 102.0) / 102.0 * 100.0)


def test_qoq_one_quarter_back():
    points = [pt("2024-01-01", 300.0), pt("2024-04-01", 309.0)]
    result = transform.qoq(points)
    assert len(result) == 1
    assert result[0].value == pytest.approx(3.0)


# --------------------------------------------------------------------------
# annualised
# --------------------------------------------------------------------------

def test_annualised_quarterly_compounds_to_four_quarters():
    # 1% q/q growth annualises to (1.01)^4 - 1 = 4.0604...%
    points = [pt("2024-01-01", 100.0), pt("2024-04-01", 101.0)]
    result = transform.annualised(points, "Q")
    assert len(result) == 1
    assert result[0].value == pytest.approx((1.01 ** 4 - 1) * 100.0)


def test_annualised_monthly_compounds_to_twelve_months():
    points = [pt("2024-01-01", 100.0), pt("2024-02-01", 100.5)]
    result = transform.annualised(points, "M")
    assert result[0].value == pytest.approx((1.005 ** 12 - 1) * 100.0)


def test_annualised_annual_is_a_no_op_yoy():
    points = [pt("2023-01-01", 100.0), pt("2024-01-01", 105.0)]
    result = transform.annualised(points, "A")
    assert result[0].value == pytest.approx(5.0)


# --------------------------------------------------------------------------
# Derived series formulas (brief §6.2)
# --------------------------------------------------------------------------

def test_real_wage_growth():
    wage_growth = [pt("2024-01-01", 4.0), pt("2024-02-01", 4.5)]
    cpi_yoy = [pt("2024-01-01", 3.0), pt("2024-02-01", 3.2)]
    result = transform.real_wage_growth(wage_growth, cpi_yoy)
    values = {p.period_start: p.value for p in result}
    assert values[date(2024, 1, 1)] == pytest.approx(1.0)
    assert values[date(2024, 2, 1)] == pytest.approx(1.3)


def test_real_policy_rate():
    policy_rate = [pt("2024-01-01", 5.25)]
    cpi_yoy = [pt("2024-01-01", 3.0)]
    result = transform.real_policy_rate(policy_rate, cpi_yoy)
    assert result[0].value == pytest.approx(2.25)


def test_misery_index():
    unemployment = [pt("2024-01-01", 4.0)]
    cpi_yoy = [pt("2024-01-01", 3.5)]
    result = transform.misery_index(unemployment, cpi_yoy)
    assert result[0].value == pytest.approx(7.5)


def test_yield_spread_domestic():
    long_yield = [pt("2024-01-01", 4.2)]
    short_yield = [pt("2024-01-01", 4.8)]
    result = transform.yield_spread_domestic(long_yield, short_yield)
    # Inverted curve -> negative spread.
    assert result[0].value == pytest.approx(-0.6)


def test_yield_spread_vs_benchmark():
    domestic = [pt("2024-01-01", 3.0)]
    benchmark = [pt("2024-01-01", 4.2)]
    result = transform.yield_spread_vs_benchmark(domestic, benchmark)
    assert result[0].value == pytest.approx(-1.2)


def test_house_price_to_income():
    hpi = [pt("2024-01-01", 150.0)]
    income = [pt("2024-01-01", 120.0)]
    result = transform.house_price_to_income(hpi, income)
    assert result[0].value == pytest.approx(125.0)


def test_fx_real_change():
    fx_yoy = [pt("2024-01-01", 5.0)]        # domestic currency depreciated 5% nominal
    domestic_cpi_yoy = [pt("2024-01-01", 2.0)]
    foreign_cpi_yoy = [pt("2024-01-01", 4.0)]
    result = transform.fx_real_change(fx_yoy, domestic_cpi_yoy, foreign_cpi_yoy)
    # 5.0 + (4.0 - 2.0) = 7.0
    assert result[0].value == pytest.approx(7.0)


def test_sahm_rule_requires_full_twelve_month_window():
    # Only 10 months of data -> not enough for a full trailing 12-month
    # window of 3-month averages (partial-year edge case).
    points = [pt(f"2024-{m:02d}-01", 4.0) for m in range(1, 11)]
    result = transform.sahm_rule(points)
    assert result == []


def test_sahm_rule_flags_rise_from_trailing_low():
    # 14 months flat at 4.0%, then unemployment climbs in the final 3
    # months. The 3-month avg trough over the trailing window is 4.0; the
    # final month's 3-month avg is (4.6+4.9+5.2)/3 = 4.9 -> rule value 0.9.
    values = [4.0] * 11 + [4.6, 4.9, 5.2]
    points = [pt(f"2023-{m:02d}-01", v) for m, v in zip(range(1, 12), values[:11])]
    points += [pt("2023-12-01", values[11]), pt("2024-01-01", values[12]), pt("2024-02-01", values[13])]
    result = transform.sahm_rule(points)
    last = result[-1]
    assert last.period_start == date(2024, 2, 1)
    assert last.value == pytest.approx(4.9 - 4.0)


# --------------------------------------------------------------------------
# compute_derived() dispatcher
# --------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """Minimal sqlite3.Connection stand-in: routes SELECTs from a
    pre-built table of dict rows, and INSERT OR IGNOREs into an in-memory
    observations list, tracking total_changes like sqlite3 does.
    """

    def __init__(self, series_rows, observations=None):
        self.series_rows = series_rows
        self.observations = observations or []
        self.total_changes = 0

    def execute(self, sql, params=()):
        sql_stripped = sql.strip()
        if sql_stripped.startswith("SELECT * FROM series"):
            series_id = params[0]
            return _FakeCursor([r for r in self.series_rows if r["series_id"] == series_id])
        if sql_stripped.startswith("SELECT frequency, transform_default FROM series"):
            series_id = params[0]
            return _FakeCursor([r for r in self.series_rows if r["series_id"] == series_id])
        if sql_stripped.startswith("SELECT period_start, value FROM observations"):
            series_id = params[0]
            rows = sorted(
                (o for o in self.observations if o["series_id"] == series_id),
                key=lambda o: o["period_start"],
            )
            return _FakeCursor(rows)
        raise AssertionError(f"unexpected SQL: {sql!r}")

    def executemany(self, sql, rows):
        for row in rows:
            series_id, period_start, period_end, frequency, value, is_preliminary, fetched_at = row
            key = (series_id, period_start)
            if any(o["series_id"] == series_id and o["period_start"] == period_start for o in self.observations):
                continue
            self.observations.append({
                "series_id": series_id, "period_start": period_start, "period_end": period_end,
                "frequency": frequency, "value": value, "is_preliminary": is_preliminary,
                "fetched_at": fetched_at,
            })
            self.total_changes += 1


def _series_row(series_id, frequency="M", transform_default="level", is_derived=0, derivation=None):
    return {
        "series_id": series_id, "frequency": frequency,
        "transform_default": transform_default, "is_derived": is_derived,
        "derivation": derivation,
    }


def test_compute_derived_writes_observations_for_known_formula():
    import json

    series_rows = [
        _series_row(
            "us.misery_index", is_derived=1,
            derivation=json.dumps({
                "formula": "misery_index",
                "components": {
                    "unemployment_rate": "us.unemployment_rate",
                    "cpi_yoy": "us.cpi_headline",
                },
            }),
        ),
        _series_row("us.unemployment_rate", frequency="M", transform_default="level"),
        _series_row("us.cpi_headline", frequency="M", transform_default="yoy"),
    ]
    observations = [
        {"series_id": "us.unemployment_rate", "period_start": "2024-01-01", "value": 4.0},
        {"series_id": "us.cpi_headline", "period_start": "2023-01-01", "value": 300.0},
        {"series_id": "us.cpi_headline", "period_start": "2024-01-01", "value": 309.0},
    ]
    conn = _FakeConn(series_rows, observations)

    written = transform.compute_derived("us.misery_index", conn)

    assert written == 1
    derived = [o for o in conn.observations if o["series_id"] == "us.misery_index"]
    assert len(derived) == 1
    # cpi_yoy for 2024-01-01 = (309-300)/300*100 = 3.0; misery = 4.0 + 3.0
    assert derived[0]["value"] == pytest.approx(7.0)


def test_compute_derived_skips_missing_series_row():
    conn = _FakeConn(series_rows=[])
    assert transform.compute_derived("us.does_not_exist", conn) == 0
    assert conn.observations == []


def test_compute_derived_skips_when_not_marked_derived():
    conn = _FakeConn(series_rows=[_series_row("us.misery_index", is_derived=0)])
    assert transform.compute_derived("us.misery_index", conn) == 0


def test_compute_derived_skips_when_no_derivation_spec():
    conn = _FakeConn(series_rows=[_series_row("us.misery_index", is_derived=1, derivation=None)])
    assert transform.compute_derived("us.misery_index", conn) == 0


def test_compute_derived_skips_when_component_has_no_data():
    import json

    series_rows = [
        _series_row(
            "us.misery_index", is_derived=1,
            derivation=json.dumps({
                "formula": "misery_index",
                "components": {"unemployment_rate": "us.unemployment_rate", "cpi_yoy": "us.cpi_headline"},
            }),
        ),
        _series_row("us.unemployment_rate"),
        _series_row("us.cpi_headline"),
    ]
    conn = _FakeConn(series_rows, observations=[])  # no observations fetched yet

    assert transform.compute_derived("us.misery_index", conn) == 0
    assert conn.observations == []


def test_compute_derived_skips_unknown_formula():
    import json

    series_rows = [
        _series_row(
            "us.mystery", is_derived=1,
            derivation=json.dumps({"formula": "not_a_real_formula", "components": {}}),
        ),
    ]
    conn = _FakeConn(series_rows)
    assert transform.compute_derived("us.mystery", conn) == 0
