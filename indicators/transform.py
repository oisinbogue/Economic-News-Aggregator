"""YoY/MoM/QoQ/annualised transforms and derived-series computation for the
indicators subsystem.

`observations` (INDICATORS_SCHEMA_PROPOSAL.md §3) stores raw fetched levels
per period_start -- it does *not* pre-compute percent changes. The
transform functions below turn a raw level series into the "as displayed"
series named by a `series.transform_default` value (yoy/mom/qoq/level/
annualised). `compute_derived()` then combines one or more *transformed*
component series into a brief-§6.2 derived series (misery_index,
real_wage_growth, etc.) and writes the result back into `observations`
under the derived series' own series_id.

Lookback is date-based (subtract N months, or subtract a year with a
Feb-29 fallback), not index-based ("go back K rows"). A gap in the source
data (a missing month/quarter) would silently shift an index-based lookback
onto the wrong period; matching by the actual calendar date instead means
a missing prior-year period simply produces no result for that date, which
is the correct behaviour for both the "partial year of history" and
"leap year" edge cases called out in the brief's testing section -- see
the leap-day fallback in `_subtract_years()`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from datetime import datetime, timezone
from typing import Callable

PERIODS_PER_YEAR = {"D": 365, "W": 52, "M": 12, "Q": 4, "A": 1}

INSERT_OBSERVATION_SQL = """
    INSERT OR IGNORE INTO observations
        (series_id, period_start, period_end, frequency, value, is_preliminary, fetched_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class Point:
    period_start: date
    value: float


def load_observations(conn, series_id: str) -> list[Point]:
    """Loads every stored (period_start, value) pair for series_id, oldest first."""
    rows = conn.execute(
        "SELECT period_start, value FROM observations WHERE series_id = ? ORDER BY period_start",
        (series_id,),
    ).fetchall()
    return [Point(date.fromisoformat(row["period_start"]), row["value"]) for row in rows]


# --------------------------------------------------------------------------
# Date arithmetic
# --------------------------------------------------------------------------

def _add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def _subtract_months(d: date, months: int) -> date:
    """Subtracts calendar months, always landing on day 1.

    Safe for the M/Q/A frequencies' period_start convention (always the
    first day of the period), so this never needs a day-clamping fallback
    the way `_subtract_years` does for arbitrary (e.g. daily) dates.
    """
    return _add_months(date(d.year, d.month, 1), -months)


def _subtract_years(d: date, years: int) -> date:
    """Subtracts whole years from an arbitrary date (used for D/W frequencies
    and for the A-frequency yoy lookback).

    Feb 29 has no equivalent in a non-leap target year -- falls back to
    Feb 28 rather than raising, so a leap-day observation still gets a
    same-period-last-year comparison instead of silently dropping out.
    """
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year - years)


def _yoy_lookback(frequency: str) -> Callable[[date], date]:
    if frequency in ("M", "Q"):
        return lambda d: _subtract_months(d, 12)
    # A, D, W: a calendar year back (period_start for A is always day 1 of
    # the year so the leap fallback never triggers there; it matters for D).
    return lambda d: _subtract_years(d, 1)


def _one_period_step(frequency: str) -> Callable[[date], date]:
    if frequency == "M":
        return lambda d: _subtract_months(d, 1)
    if frequency == "Q":
        return lambda d: _subtract_months(d, 3)
    if frequency == "A":
        return lambda d: _subtract_years(d, 1)
    if frequency == "W":
        return lambda d: d - timedelta(days=7)
    return lambda d: d - timedelta(days=1)  # D


# --------------------------------------------------------------------------
# Core transforms
# --------------------------------------------------------------------------

def _pct_change_series(points: list[Point], lookback: Callable[[date], date]) -> list[Point]:
    ordered = sorted(points, key=lambda p: p.period_start)
    by_period = {p.period_start: p.value for p in ordered}
    result = []
    for p in ordered:
        prior_value = by_period.get(lookback(p.period_start))
        if prior_value is None or prior_value == 0:
            continue  # no matching prior period (partial-year history) or undefined base
        result.append(Point(p.period_start, (p.value - prior_value) / abs(prior_value) * 100.0))
    return result


def yoy(points: list[Point], frequency: str) -> list[Point]:
    """Year-over-year percent change. Quarterly data compares 4 periods back
    (i.e. 12 months, via date arithmetic), monthly compares 12 periods back --
    both expressed as "the same calendar month/quarter one year earlier"
    rather than a fixed row-count, so gaps in the source data don't shift
    the comparison onto the wrong period.
    """
    return _pct_change_series(points, _yoy_lookback(frequency))


def mom(points: list[Point]) -> list[Point]:
    """Month-over-month percent change (one calendar month back)."""
    return _pct_change_series(points, lambda d: _subtract_months(d, 1))


def qoq(points: list[Point]) -> list[Point]:
    """Quarter-over-quarter percent change (one calendar quarter, 3 months, back)."""
    return _pct_change_series(points, lambda d: _subtract_months(d, 3))


def annualised(points: list[Point], frequency: str) -> list[Point]:
    """Compounds a single-period growth rate up to an annualised rate, e.g.
    the US convention of reporting quarterly GDP growth as if it continued
    for 4 quarters: (1 + q/q)^4 - 1. For frequency='A' this is a no-op
    (periods_per_year == 1) and just returns the year-over-year change.
    """
    periods_per_year = PERIODS_PER_YEAR[frequency]
    step_changes = _pct_change_series(points, _one_period_step(frequency))
    return [
        Point(p.period_start, ((1 + p.value / 100.0) ** periods_per_year - 1) * 100.0)
        for p in step_changes
    ]


TRANSFORM_FUNCS: dict[str, Callable[[list[Point], str], list[Point]]] = {
    "yoy": yoy,
    "mom": lambda points, frequency: mom(points),
    "qoq": lambda points, frequency: qoq(points),
    "annualised": annualised,
    "level": lambda points, frequency: sorted(points, key=lambda p: p.period_start),
}


def apply_transform(points: list[Point], transform_name: str, frequency: str) -> list[Point]:
    func = TRANSFORM_FUNCS.get(transform_name)
    if func is None:
        raise ValueError(f"unknown transform {transform_name!r}")
    return func(points, frequency)


# --------------------------------------------------------------------------
# Derived series (brief §6.2)
#
# Each formula takes one or more *already-transformed* component series
# (the series' own transform_default already applied -- e.g. a cpi_yoy
# argument is the output of yoy() on the raw CPI level series, not the raw
# levels themselves) and returns a new Point series aligned on the
# component periods they have in common.
# --------------------------------------------------------------------------

def _combine(*series: list[Point], op: Callable[..., float]) -> list[Point]:
    """Aligns N Point series on shared period_start values and applies op
    positionally to their values. Periods present in only some of the
    series (different history lengths / frequencies) are skipped.
    """
    if not series:
        return []
    by_period_list = [{p.period_start: p.value for p in s} for s in series]
    common_periods = set(by_period_list[0].keys())
    for by_period in by_period_list[1:]:
        common_periods &= by_period.keys()
    return [
        Point(period, op(*(by_period[period] for by_period in by_period_list)))
        for period in sorted(common_periods)
    ]


def real_wage_growth(wage_growth: list[Point], cpi_yoy: list[Point]) -> list[Point]:
    """Real wage growth = nominal wage growth (YoY %) - CPI inflation (YoY %)."""
    return _combine(wage_growth, cpi_yoy, op=lambda w, c: w - c)


def real_policy_rate(policy_rate: list[Point], cpi_yoy: list[Point]) -> list[Point]:
    """Ex-post real policy rate (Fisher approximation) = nominal policy rate
    level - CPI inflation (YoY %)."""
    return _combine(policy_rate, cpi_yoy, op=lambda r, c: r - c)


def misery_index(unemployment_rate: list[Point], cpi_yoy: list[Point]) -> list[Point]:
    """Classic (Okun) misery index = unemployment rate + CPI inflation (YoY %)."""
    return _combine(unemployment_rate, cpi_yoy, op=lambda u, c: u + c)


def yield_spread_domestic(long_yield: list[Point], short_yield: list[Point]) -> list[Point]:
    """Domestic yield curve spread = long-tenor yield - short-tenor yield
    (e.g. 10y - 2y), both for the same country."""
    return _combine(long_yield, short_yield, op=lambda long, short: long - short)


def yield_spread_vs_benchmark(domestic_yield: list[Point], benchmark_yield: list[Point]) -> list[Point]:
    """Cross-country yield spread = domestic yield - benchmark yield
    (e.g. a country's 10y minus the US 10y or German Bund)."""
    return _combine(domestic_yield, benchmark_yield, op=lambda d, b: d - b)


def house_price_to_income(house_price_index: list[Point], income_index: list[Point]) -> list[Point]:
    """House price affordability ratio = house price index / income index * 100."""
    return _combine(house_price_index, income_index, op=lambda hpi, income: (hpi / income) * 100.0)


def fx_real_change(
    fx_yoy: list[Point], domestic_cpi_yoy: list[Point], foreign_cpi_yoy: list[Point]
) -> list[Point]:
    """Real exchange rate change (PPP-adjusted) = nominal FX YoY change +
    (foreign inflation YoY - domestic inflation YoY). `fx_yoy` is assumed to
    be the YoY change in domestic-currency-per-foreign-currency units, so a
    positive value is domestic depreciation.
    """
    return _combine(
        fx_yoy, domestic_cpi_yoy, foreign_cpi_yoy,
        op=lambda fx, dom, foreign: fx + (foreign - dom),
    )


def sahm_rule(unemployment_rate: list[Point]) -> list[Point]:
    """Sahm Rule recession indicator: the 3-month moving average of the
    unemployment rate, minus the minimum of that 3-month moving average
    over the trailing 12 months (inclusive of the current month).

    Requires at least 14 months of monthly unemployment_rate history (2
    months to seed the first 3-month average, plus a full 12-month trailing
    window) -- shorter (partial-year) input simply yields no points rather
    than a value computed against an incomplete window.
    """
    ordered = sorted(unemployment_rate, key=lambda p: p.period_start)
    dates = [p.period_start for p in ordered]
    values = [p.value for p in ordered]
    n = len(ordered)

    three_month_avg: list[float | None] = [None] * n
    for i in range(n):
        if i >= 2:
            three_month_avg[i] = sum(values[i - 2 : i + 1]) / 3.0

    result = []
    for i in range(n):
        if three_month_avg[i] is None:
            continue
        window_start = i - 11
        if window_start < 0:
            continue  # not a full trailing 12-month window yet
        window = [v for v in three_month_avg[window_start : i + 1] if v is not None]
        if len(window) < 12:
            continue
        result.append(Point(dates[i], three_month_avg[i] - min(window)))
    return result


FORMULA_REGISTRY: dict[str, Callable[..., list[Point]]] = {
    "real_wage_growth": real_wage_growth,
    "real_policy_rate": real_policy_rate,
    "misery_index": misery_index,
    "yield_spread_domestic": yield_spread_domestic,
    "yield_spread_vs_benchmark": yield_spread_vs_benchmark,
    "sahm_rule": sahm_rule,
    "house_price_to_income": house_price_to_income,
    "fx_real_change": fx_real_change,
}


_FREQUENCY_PERIOD_END: dict[str, Callable[[date], date]] = {
    "A": lambda start: date(start.year + 1, 1, 1) - timedelta(days=1),
    "Q": lambda start: _add_months(start, 3) - timedelta(days=1),
    "M": lambda start: _add_months(start, 1) - timedelta(days=1),
    "W": lambda start: start + timedelta(days=6),
    "D": lambda start: start,
}


def _period_end(period_start: date, frequency: str) -> date:
    compute = _FREQUENCY_PERIOD_END.get(frequency)
    return compute(period_start) if compute else period_start


def compute_derived(series_id: str, conn) -> int:
    """Computes and stores observations for one derived series.

    `derivation` (a JSON TEXT column on `series`, per
    INDICATORS_SCHEMA_PROPOSAL.md §3) is expected to look like:

        {"formula": "misery_index",
         "components": {"unemployment_rate": "us.unemployment_rate",
                         "cpi_yoy": "us.cpi_headline"}}

    Each `components` value is the series_id of an *already-fetched*
    component series; its own `transform_default` (from the `series` table)
    is applied before it's passed into the formula function under the
    matching keyword argument.

    Nothing in config/indicators/*.yaml populates `derivation` yet (Phase 1
    per INDICATORS_SCHEMA_PROPOSAL.md §4/§5), so this is written to fail
    soft: any of series_id not existing, not being marked is_derived,
    having no/invalid derivation spec, an unknown formula name, or a
    component series with no data yet is logged and skipped -- never
    raised -- rather than aborting a build/fetch run.

    Returns the number of new observation rows written (0 on any skip).
    """
    row = conn.execute("SELECT * FROM series WHERE series_id = ?", (series_id,)).fetchone()
    if row is None:
        print(f"compute_derived: skipping {series_id!r}: no series row")
        return 0
    if not row["is_derived"]:
        print(f"compute_derived: skipping {series_id!r}: not marked is_derived")
        return 0
    if not row["derivation"]:
        print(f"compute_derived: skipping {series_id!r}: no derivation spec yet")
        return 0

    try:
        spec = json.loads(row["derivation"])
    except (TypeError, ValueError) as exc:
        print(f"compute_derived: skipping {series_id!r}: invalid derivation JSON: {exc}")
        return 0

    formula = FORMULA_REGISTRY.get(spec.get("formula"))
    if formula is None:
        print(f"compute_derived: skipping {series_id!r}: unknown formula {spec.get('formula')!r}")
        return 0

    kwargs: dict[str, list[Point]] = {}
    for arg_name, component_series_id in spec.get("components", {}).items():
        component_row = conn.execute(
            "SELECT frequency, transform_default FROM series WHERE series_id = ?",
            (component_series_id,),
        ).fetchone()
        if component_row is None:
            print(f"compute_derived: skipping {series_id!r}: component series {component_series_id!r} not found")
            return 0

        raw_points = load_observations(conn, component_series_id)
        if not raw_points:
            print(f"compute_derived: skipping {series_id!r}: no observations yet for {component_series_id!r}")
            return 0

        kwargs[arg_name] = apply_transform(raw_points, component_row["transform_default"], component_row["frequency"])

    result_points = formula(**kwargs)
    if not result_points:
        return 0

    fetched_at = datetime.now(timezone.utc).isoformat()
    frequency = row["frequency"]
    rows = [
        (series_id, p.period_start.isoformat(), _period_end(p.period_start, frequency).isoformat(), frequency, p.value, 0, fetched_at)
        for p in result_points
    ]
    before = conn.total_changes
    conn.executemany(INSERT_OBSERVATION_SQL, rows)
    return conn.total_changes - before


def compute_all_derived(conn) -> dict[str, int]:
    """Runs `compute_derived()` for every active is_derived=1 series.

    Must run after `indicators.fetch` -- component series need fresh
    observations already written, same ordering
    `tests/test_indicators_build.py`'s smoke test exercises. Returns
    {series_id: rows_written}, including entries where compute_derived()
    skipped (0 rows written) so a caller can tell which derived series
    produced nothing this run without re-deriving why.
    """
    series_ids = [
        row["series_id"]
        for row in conn.execute("SELECT series_id FROM series WHERE is_derived = 1 AND active = 1")
    ]
    return {series_id: compute_derived(series_id, conn) for series_id in series_ids}


def main() -> dict[str, int]:
    from indicators.db import get_connection, init_db

    init_db()
    with get_connection() as conn:
        results = compute_all_derived(conn)

    total_new = sum(results.values())
    print(f"Computed {len(results)} derived series, {total_new} new observation(s) written.")
    sys.stdout.flush()
    return results


if __name__ == "__main__":
    main()
