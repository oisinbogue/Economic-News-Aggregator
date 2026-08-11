"""Fetches new observations for every active series and writes them into
the `observations` table.

For each active series (`series` table, active=1):
  1. Dispatches to `indicators/providers/<series.provider>.py`'s `fetch()`
     coroutine, bounded by a *per-provider* semaphore
     (`indicators.provider_concurrency`, see `_provider_semaphore()`) plus a
     polite minimum delay before each request to that provider
     (`indicators.provider_delay_seconds`) -- INDICATORS_BRIEF.md §8's
     "rate-limit safe" requirement. `indicators/http.py` layers exponential
     backoff on top of that for individual 429/5xx responses.
  2. New observations are inserted; an observation for a (series_id,
     period_start) already in the db whose value has *changed* -- a
     preliminary print later revised -- is UPDATEd in place and the old/new
     pair is logged to `observation_revisions` rather than silently
     dropped (see `_store_observations()`). An unchanged value is a no-op,
     same as the old INSERT OR IGNORE behaviour.
  3. One `fetch_log` row per series per run, status ok/stale/error:
       - ok: the provider call succeeded (new observations may be zero if
         everything it returned was already in the db).
       - stale: the provider call succeeded but returned zero observations
         -- not an exception, but worth flagging (empty series or a
         provider_series_id that no longer resolves to any data).
       - error: the provider call raised, or no provider module exists for
         `series.provider`. Caught per-series so one series' failure can
         never abort the run.

Two modes (INDICATORS_BRIEF.md §8/§13 deliverable 4):
  - Incremental (default): only fetches series that are "due" per
    `_is_due_for_refresh_release_aware()` below. A series with a
    `release_schedule` is polled aggressively in the window around its next
    scheduled release (from the `releases` table, populated by
    `indicators/calendar.py`) and backed off far more the rest of the
    time -- a series whose next release is six weeks away is not
    refetched every run. A series with no release_schedule yet (most of
    them, still -- see INDICATORS.md) falls back to the coarser
    frequency-only heuristic `_is_due_for_refresh()` always used.
  - Backfill (`--backfill`): fetches every active series unconditionally,
    ignoring the due-for-refresh check. Intended for the workflow_dispatch-
    only backfill job (a first-time population, or catching up series the
    incremental check has been skipping), not for the scheduled run.

Usage: python -m indicators.fetch [--backfill]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from datetime import datetime, timedelta, timezone

from indicators import calendar as calendar_mod
from indicators import fx
from indicators.config import get_config
from indicators.db import get_connection, init_db
from indicators.export import _next_release_by_series

# Coarse frequency-based fallback for a series with no release_schedule yet
# (still most of them -- release_schedule is nullable, see INDICATORS.md).
# Deliberately coarser than the release-aware windows below -- a cheap
# heuristic, not release-awareness.
FREQUENCY_REFRESH_INTERVAL: dict[str, timedelta] = {
    "D": timedelta(hours=20),
    "W": timedelta(days=3),
    "M": timedelta(days=7),
    "Q": timedelta(days=21),
    "A": timedelta(days=60),
}

# Release-aware windows (only used when a series has a release_schedule and
# therefore a `releases` row to compare `now` against). Within
# [-RELEASE_WINDOW_AFTER, +RELEASE_WINDOW_BEFORE] of the scheduled release,
# poll aggressively (data may land early, on time, or a little late); well
# outside that window, back off hard -- this is the mechanism that satisfies
# "a series whose next release is six weeks away must not be refetched
# every run" (brief §8).
RELEASE_WINDOW_BEFORE = timedelta(days=3)
RELEASE_WINDOW_AFTER = timedelta(days=7)

AGGRESSIVE_REFRESH_INTERVAL = timedelta(hours=6)

FAR_FROM_RELEASE_REFRESH_INTERVAL: dict[str, timedelta] = {
    "D": timedelta(hours=20),
    "W": timedelta(days=3),
    "M": timedelta(days=10),
    "Q": timedelta(days=30),
    "A": timedelta(days=90),
}

# Per-provider concurrency/delay fallbacks for a provider not listed in
# config.yaml's indicators.provider_concurrency/provider_delay_seconds.
DEFAULT_PROVIDER_CONCURRENCY = 3
DEFAULT_PROVIDER_DELAY = 0.3

# How much an existing observation's value may differ from a refetched one
# before it counts as a genuine revision rather than float round-trip noise.
REVISION_EPSILON = 1e-9

INSERT_OBSERVATION_SQL = """
    INSERT INTO observations
        (series_id, period_start, period_end, frequency, value, value_usd, is_preliminary, fetched_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

UPDATE_OBSERVATION_SQL = """
    UPDATE observations
    SET period_end = ?, value = ?, value_usd = ?, is_preliminary = ?, fetched_at = ?
    WHERE series_id = ? AND period_start = ?
"""

INSERT_REVISION_SQL = """
    INSERT INTO observation_revisions (series_id, period_start, old_value, new_value, observed_at)
    VALUES (?, ?, ?, ?, ?)
"""


def _load_provider(provider_name: str):
    return importlib.import_module(f"indicators.providers.{provider_name}")


def _store_observations(
    conn,
    series_id: str,
    frequency: str,
    rows: list[tuple],
    fetched_at: str,
) -> int:
    """Writes one series' freshly-fetched observations into `observations`,
    logging a revision for any (series_id, period_start) whose value
    changed since it was last stored.

    `rows` is a list of (period_start_iso, period_end_iso, value,
    value_usd, is_preliminary) tuples. A period_start not yet in the db is
    a plain insert; one already there with an identical value (the common
    case on every run after the first -- most of a series' history never
    changes) is left untouched; one already there with a *different* value
    is UPDATEd in place and the (old_value, new_value) pair is appended to
    `observation_revisions` -- this is the brief's "wire up
    observation_revisions properly" deliverable (Phase 1 had a
    TODO here and used INSERT OR IGNORE, which silently dropped revised
    prints).

    Returns the count of genuinely new observations written (unchanged
    from the old INSERT OR IGNORE-based return value's meaning); revisions
    are not included in this count since callers use it purely for "how
    much new data did this run add" reporting.
    """
    new_count = 0
    for period_start, period_end, value, value_usd, is_preliminary in rows:
        existing = conn.execute(
            "SELECT value FROM observations WHERE series_id = ? AND period_start = ?",
            (series_id, period_start),
        ).fetchone()

        if existing is None:
            conn.execute(
                INSERT_OBSERVATION_SQL,
                (series_id, period_start, period_end, frequency, value, value_usd, is_preliminary, fetched_at),
            )
            new_count += 1
            continue

        if abs(existing["value"] - value) > REVISION_EPSILON:
            conn.execute(
                INSERT_REVISION_SQL,
                (series_id, period_start, existing["value"], value, fetched_at),
            )
            conn.execute(
                UPDATE_OBSERVATION_SQL,
                (period_end, value, value_usd, is_preliminary, fetched_at, series_id, period_start),
            )

    return new_count


async def fetch_series(
    series: dict,
    semaphore: asyncio.Semaphore,
    delay_seconds: float,
) -> tuple[str, str, str | None, int]:
    """Fetches and stores one series' observations.

    Returns (series_id, status, detail, new_observation_count).
    """
    series_id = series["series_id"]

    async with semaphore:
        try:
            provider_module = _load_provider(series["provider"])
        except ModuleNotFoundError:
            return series_id, "error", f"no provider module for provider={series['provider']!r}", 0

        if delay_seconds:
            await asyncio.sleep(delay_seconds)

        try:
            observations = await provider_module.fetch(series)
        except Exception as exc:  # a single series must never abort the run
            return series_id, "error", str(exc), 0

    if not observations:
        return series_id, "stale", "provider returned zero observations", 0

    fetched_at = datetime.now(timezone.utc).isoformat()
    unit_currency = series.get("unit_currency") if series.get("unit") == "currency" else None

    with get_connection() as conn:
        rows = [
            (
                obs.period_start.isoformat(),
                obs.period_end.isoformat(),
                obs.value,
                fx.convert(obs.value, unit_currency, "USD", obs.period_start, conn) if unit_currency else None,
                int(obs.is_preliminary),
            )
            for obs in observations
        ]
        new_count = _store_observations(conn, series_id, series["frequency"], rows, fetched_at)

    return series_id, "ok", None, new_count


# --------------------------------------------------------------------------
# Due-for-refresh selection
# --------------------------------------------------------------------------

def _is_due_for_refresh(frequency: str, last_ok_at: datetime | None, now: datetime) -> bool:
    """Frequency-only fallback: whether a series with no release_schedule
    (and therefore no `releases` row to compare against) should be
    refetched in incremental (non-backfill) mode.

    A series with no prior successful fetch is always due (covers both a
    brand-new series and this being the very first run ever). Otherwise
    it's due once at least `FREQUENCY_REFRESH_INTERVAL[frequency]` has
    passed since the last successful fetch.
    """
    if last_ok_at is None:
        return True
    interval = FREQUENCY_REFRESH_INTERVAL.get(frequency, timedelta(days=1))
    return now - last_ok_at >= interval


def _is_due_for_refresh_release_aware(
    frequency: str,
    last_ok_at: datetime | None,
    next_release_at: datetime | None,
    now: datetime,
) -> bool:
    """Release-aware due check (INDICATORS_BRIEF.md §8): a series with a
    known next scheduled release is polled aggressively
    (AGGRESSIVE_REFRESH_INTERVAL) inside the window
    [-RELEASE_WINDOW_AFTER, +RELEASE_WINDOW_BEFORE] around it, and backed
    off to FAR_FROM_RELEASE_REFRESH_INTERVAL (much coarser than the plain
    frequency heuristic) outside it. Falls back to `_is_due_for_refresh()`
    when `next_release_at` is None -- no `releases` row yet, i.e. the
    series has no release_schedule configured.
    """
    if last_ok_at is None:
        return True
    if next_release_at is None:
        return _is_due_for_refresh(frequency, last_ok_at, now)

    delta = next_release_at - now
    if -RELEASE_WINDOW_AFTER <= delta <= RELEASE_WINDOW_BEFORE:
        interval = AGGRESSIVE_REFRESH_INTERVAL
    else:
        interval = FAR_FROM_RELEASE_REFRESH_INTERVAL.get(frequency, FREQUENCY_REFRESH_INTERVAL.get(frequency, timedelta(days=1)))
    return now - last_ok_at >= interval


def _select_due_series(conn, series_rows: list[dict], now: datetime) -> list[dict]:
    """Filters `series_rows` down to those due for an incremental refetch,
    per `_is_due_for_refresh_release_aware()`. Looks up each series' most
    recent status='ok' `fetch_log` row (the only record of "when did this
    series last succeed" available) and its nearest still-scheduled
    `releases` row, if any.
    """
    next_releases = _next_release_by_series(conn)  # series_id -> scheduled_at ISO string

    due = []
    for row in series_rows:
        last_ok_row = conn.execute(
            "SELECT MAX(run_at) AS run_at FROM fetch_log WHERE series_id = ? AND status = 'ok'",
            (row["series_id"],),
        ).fetchone()
        last_ok_at = (
            datetime.fromisoformat(last_ok_row["run_at"])
            if last_ok_row and last_ok_row["run_at"] else None
        )
        next_release_iso = next_releases.get(row["series_id"])
        next_release_at = datetime.fromisoformat(next_release_iso) if next_release_iso else None

        if _is_due_for_refresh_release_aware(row["frequency"], last_ok_at, next_release_at, now):
            due.append(row)
    return due


# --------------------------------------------------------------------------
# fx.py refresh
# --------------------------------------------------------------------------

async def _refresh_fx_rates(series_rows: list[dict]) -> None:
    """Refreshes `fx_rates` before any series is fetched, so `fetch_series`'s
    value_usd conversion below has same-run data to work with. ECB first
    (covers most active currencies); Stooq fills in any unit_currency among
    active currency-unit series that the ECB feed didn't return a rate for.
    Failures here are logged, never raised -- a stale/missing fx_rates row
    just means value_usd stays null for that observation, not that the
    whole fetch run aborts.
    """
    try:
        ecb_rows = await fx.fetch_ecb_rates()
    except Exception as exc:
        print(f"fx: ECB fetch failed, currency conversions may be incomplete: {exc}")
        ecb_rows = []

    covered = {quote for _, _, quote, _ in ecb_rows}
    needed = {
        row["unit_currency"]
        for row in series_rows
        if row.get("unit") == "currency" and row.get("unit_currency")
    } - covered - {"EUR"}

    stooq_rows = []
    for currency in needed:
        try:
            row = await fx.fetch_stooq_rate(currency)
        except Exception as exc:
            print(f"fx: Stooq fallback failed for {currency}: {exc}")
            continue
        if row is not None:
            stooq_rows.append(row)

    with get_connection() as conn:
        fx.store_fx_rates(conn, ecb_rows + stooq_rows)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _provider_config(cfg: dict) -> tuple[dict, dict]:
    indicators_cfg = cfg.get("indicators", {})
    return (
        indicators_cfg.get("provider_concurrency", {}) or {},
        indicators_cfg.get("provider_delay_seconds", {}) or {},
    )


async def fetch_all(concurrency: int, backfill: bool = False) -> list[tuple[str, str, str | None, int]]:
    cfg = get_config()
    provider_concurrency, provider_delay = _provider_config(cfg)

    with get_connection() as conn:
        # Ensures every active series with a release_schedule has an
        # up-to-date `releases` row for `_select_due_series()`
        # (release-unaware series are unaffected -- see calendar.py's
        # docstring, it only touches series with release_schedule set).
        calendar_mod.populate_releases(conn)

        series_rows = [dict(row) for row in conn.execute("SELECT * FROM series WHERE active = 1")]
        if not backfill:
            series_rows = _select_due_series(conn, series_rows, datetime.now(timezone.utc))

    await _refresh_fx_rates(series_rows)

    # One semaphore + delay per provider actually in use this run, not one
    # shared across every provider -- so a slow/strict provider (BoE,
    # Stooq) backing off doesn't stall a fast one (DBnomics) sharing the
    # same run (INDICATORS_BRIEF.md §8: "per-provider concurrency caps").
    semaphores: dict[str, asyncio.Semaphore] = {}
    for row in series_rows:
        provider = row["provider"]
        if provider not in semaphores:
            cap = provider_concurrency.get(provider, min(concurrency, DEFAULT_PROVIDER_CONCURRENCY))
            semaphores[provider] = asyncio.Semaphore(max(cap, 1))

    results = await asyncio.gather(
        *(
            fetch_series(
                series,
                semaphores[series["provider"]],
                provider_delay.get(series["provider"], DEFAULT_PROVIDER_DELAY),
            )
            for series in series_rows
        )
    )

    run_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.executemany(
            "INSERT INTO fetch_log (series_id, run_at, status, detail) VALUES (?, ?, ?, ?)",
            [(series_id, run_at, status, detail) for series_id, status, detail, _ in results],
        )

    return results


def main() -> list[tuple[str, str, str | None, int]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "Fetch every active series unconditionally (full historical pull), "
            "ignoring the incremental due-for-refresh check. For the "
            "workflow_dispatch-only backfill job, not the scheduled run."
        ),
    )
    args = parser.parse_args()

    init_db()
    cfg = get_config()
    concurrency = cfg["indicators"].get("fetch_concurrency", 5)

    mode = "backfill" if args.backfill else "incremental"
    print(f"Fetching active series ({mode} mode, {concurrency} at a time per provider)...")
    results = asyncio.run(fetch_all(concurrency, backfill=args.backfill))

    ok = sum(1 for _, status, _, _ in results if status == "ok")
    stale = sum(1 for _, status, _, _ in results if status == "stale")
    errors = [(sid, detail) for sid, status, detail, _ in results if status == "error"]
    new_observations = sum(count for _, _, _, count in results)

    print(
        f"Done ({mode}): {len(results)} series tried, {ok} ok, {stale} stale, "
        f"{len(errors)} error(s), {new_observations} new observation(s)."
    )
    for series_id, detail in errors:
        print(f"  error: {series_id}: {detail}")
    sys.stdout.flush()

    return results


if __name__ == "__main__":
    main()
