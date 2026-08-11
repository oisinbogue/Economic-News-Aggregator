"""Stooq daily-history CSV provider (https://stooq.com/q/d/l/).

`series.provider_series_id` is a bare Stooq ticker, e.g. "^dax" for the
DAX -- this module backs config/indicators/series/*.yaml's `equity_index`
entries, all four of which (^ftm, ^isq, ^sx5e, ^dax) currently carry a
"confirm exact symbol before go-live" comment.

That confirmation could not be completed live: as indicators/fx.py's own
module docstring already documents (confirmed live 2026-08-10), Stooq now
gates .../q/d/l/ behind a JS proof-of-work bot check and returns an HTML
challenge page instead of CSV to a plain HTTP client -- this was
reconfirmed live 2026-08-10 for all four tickers above (each returned
`HTTP 200` with a `<!DOCTYPE html>...noscript>This site requires
JavaScript...` body, never CSV), and even a browser-driven request hit the
same wall. There is no way to verify these ticker symbols against a live
response right now; they are left as-is in config/indicators/series/*.yaml
pending either Stooq lifting the gate or a different confirmable source
for these four indices.

This module is therefore written the same way indicators/fx.py's
`_parse_stooq_csv`/`fetch_stooq_rate` already handle that gate: a response
that isn't actually CSV (missing the Date/Close header) is treated as "no
data" and skipped, never raised -- indicators/fetch.py then logs the
series as status='stale' (zero observations) rather than status='error',
which is the accurate characterization of "the fallback isn't available
right now", not "something broke".

Unlike fx.py's `fetch_stooq_rate` (which only wants the single latest FX
rate for `fx.convert`), this provider returns Stooq's *entire* daily
history for the ticker, since observations.py needs to backfill an
indicator's full time series like every other provider here.

Response shape when Stooq *does* return real data: CSV with header
`Date,Open,High,Low,Close,Volume` (one extra column vs. the FX case's
`Date,Open,High,Low,Close|`); only `Date` and `Close` are used. Frequency
is always "D" for the `equity_index` series this module backs, so
`period_end` is always equal to `period_start`, matching dbnomics.py's "D"
case.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import httpx

from indicators.config import get_config
from indicators.providers import Observation

STOOQ_CSV_URL = "https://stooq.com/q/d/l/"


def _parse_stooq_csv(csv_text: str) -> list[tuple[date, float]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or "Date" not in reader.fieldnames or "Close" not in reader.fieldnames:
        return []

    rows: list[tuple[date, float]] = []
    for row in reader:
        raw_date = row.get("Date")
        raw_close = row.get("Close")
        if not raw_date or raw_close in (None, "", "N/D"):
            continue
        try:
            period = date.fromisoformat(raw_date)
            value = float(raw_close)
        except ValueError:
            continue
        rows.append((period, value))
    return rows


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches every available daily observation for one ticker from Stooq.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` is used. Returns an empty list (never raises) if
    Stooq's bot check intercepts the request -- see module docstring.
    """
    cfg = get_config()
    timeout = cfg["indicators"].get("fetch_timeout_seconds", 20)
    symbol = series_spec["provider_series_id"]

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(STOOQ_CSV_URL, params={"s": symbol, "i": "d"}, follow_redirects=True)
        resp.raise_for_status()

    rows = _parse_stooq_csv(resp.text)
    return [
        Observation(period_start=period, period_end=period, value=value, is_preliminary=False)
        for period, value in rows
    ]
