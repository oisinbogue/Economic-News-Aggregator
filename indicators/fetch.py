"""Fetches new observations for every active series and writes them into
the `observations` table.

For each active series (`series` table, active=1):
  1. Dispatches to `indicators/providers/<series.provider>.py`'s `fetch()`
     coroutine, bounded by `indicators.fetch_concurrency` concurrent calls
     in flight at once (mirrors pipeline/fetch.py's semaphore convention).
  2. New observations are inserted with INSERT OR IGNORE on the
     (series_id, period_start) primary key -- a period already in the db
     is left untouched here, never overwritten.
     TODO(observation_revisions): INDICATORS_SCHEMA_PROPOSAL.md's
     `observation_revisions` table exists for recording a provider
     returning a *changed* value for a period_start already stored (e.g. a
     preliminary GDP print later revised), but nothing populates it yet --
     a revised value for an already-stored period is currently silently
     ignored by INSERT OR IGNORE rather than detected and logged. Wiring
     that up is a follow-up session.
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
    `_is_due_for_refresh()` below -- a series with no prior successful fetch
    is always due; otherwise it's due once roughly one release cycle (per
    its frequency) has passed since its last successful fetch. This exists
    to avoid hammering DBnomics with a full-history refetch of hundreds of
    series on every run when most of them could not plausibly have new
    data -- DBnomics itself has no incremental/since-date query parameter,
    it always returns a series' complete history regardless of mode.
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

from indicators import fx
from indicators.config import get_config
from indicators.db import get_connection, init_db

# How long after a series' last successful fetch it becomes due for another
# incremental refetch. Deliberately coarser than the release calendar Phase
# 3 will add (release_schedule is still nullable) -- this is a cheap
# frequency-based heuristic, not release-awareness.
FREQUENCY_REFRESH_INTERVAL: dict[str, timedelta] = {
    "D": timedelta(hours=20),
    "W": timedelta(days=3),
    "M": timedelta(days=7),
    "Q": timedelta(days=21),
    "A": timedelta(days=60),
}

INSERT_OBSERVATION_SQL = """
    INSERT OR IGNORE INTO observations
        (series_id, period_start, period_end, frequency, value, value_usd, is_preliminary, fetched_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def _load_provider(provider_name: str):
    return importlib.import_module(f"indicators.providers.{provider_name}")


async def fetch_series(series: dict, semaphore: asyncio.Semaphore) -> tuple[str, str, str | None, int]:
    """Fetches and stores one series' observations.

    Returns (series_id, status, detail, new_observation_count).
    """
    series_id = series["series_id"]

    async with semaphore:
        try:
            provider_module = _load_provider(series["provider"])
        except ModuleNotFoundError:
            return series_id, "error", f"no provider module for provider={series['provider']!r}", 0

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
                series_id,
                obs.period_start.isoformat(),
                obs.period_end.isoformat(),
                series["frequency"],
                obs.value,
                fx.convert(obs.value, unit_currency, "USD", obs.period_start, conn) if unit_currency else None,
                int(obs.is_preliminary),
                fetched_at,
            )
            for obs in observations
        ]

        before = conn.total_changes
        conn.executemany(INSERT_OBSERVATION_SQL, rows)
        new_count = conn.total_changes - before

    return series_id, "ok", None, new_count


def _is_due_for_refresh(frequency: str, last_ok_at: datetime | None, now: datetime) -> bool:
    """Whether a series should be refetched in incremental (non-backfill) mode.

    A series with no prior successful fetch is always due (covers both a
    brand-new series and this being the very first run ever). Otherwise
    it's due once at least `FREQUENCY_REFRESH_INTERVAL[frequency]` has
    passed since the last successful fetch.
    """
    if last_ok_at is None:
        return True
    interval = FREQUENCY_REFRESH_INTERVAL.get(frequency, timedelta(days=1))
    return now - last_ok_at >= interval


def _select_due_series(conn, series_rows: list[dict], now: datetime) -> list[dict]:
    """Filters `series_rows` down to those due for an incremental refetch,
    per `_is_due_for_refresh()`. Looks up each series' most recent
    status='ok' `fetch_log` row itself rather than requiring the caller to
    pass that in, since fetch_log is the only record of "when did this
    series last succeed" available in Phase 1 (no release calendar yet).
    """
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
        if _is_due_for_refresh(row["frequency"], last_ok_at, now):
            due.append(row)
    return due


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


async def fetch_all(concurrency: int, backfill: bool = False) -> list[tuple[str, str, str | None, int]]:
    with get_connection() as conn:
        series_rows = [dict(row) for row in conn.execute("SELECT * FROM series WHERE active = 1")]
        if not backfill:
            series_rows = _select_due_series(conn, series_rows, datetime.now(timezone.utc))

    await _refresh_fx_rates(series_rows)

    semaphore = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(*(fetch_series(series, semaphore) for series in series_rows))

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
    print(f"Fetching active series ({mode} mode, {concurrency} at a time)...")
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
