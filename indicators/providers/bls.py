"""US Bureau of Labor Statistics public API v2 provider
(https://api.bls.gov/publicAPI/v2/timeseries/data/).

Migrated off DBnomics' `BLS/cu`, `BLS/ln`, `BLS/ce` mirrors 2026-08-13:
those mirrors had stopped tracking upstream (frozen at Jan 2025, ~559 days
stale by the time this was investigated) while BLS's own API is current --
confirmed live against CUSR0000SA0 (cpi_headline), CUSR0000SA0L1E
(cpi_core), LNS14000000 (unemployment_rate) and CES0500000003
(wages_nominal), all returning data through July 2026.

`series.provider_series_id` is the bare BLS series id, e.g.
"CUSR0000SA0" -- these are the same ids the old DBnomics mapping used
(DBnomics' `BLS/cu/CUSR0000SA0` etc. is itself just "BLS dataset cu,
series CUSR0000SA0"), so no id remapping was needed, only the provider
value in config/indicators/series/us.yaml.

Response shape (confirmed live 2026-08-13): {"status": "REQUEST_SUCCEEDED",
"Results": {"series": [{"seriesID": "...", "data": [{"year": "2026",
"period": "M07", "periodName": "July", "value": "332.813",
"footnotes": [...]}, ...]}]}}. `period` is "M01".."M12" for monthly data
(our three series are all monthly) plus an "M13" annual-average row on
some series, which is skipped here since it isn't a monthly observation
and would collide with period_start=Jan of the same year. `value` is a
string; BLS uses "-" for a footnoted/unavailable month (e.g. the 2025
government-shutdown gap seen live on CUSR0000SA0's October 2025 row) --
skipped rather than raising, same as ons.py's not-yet-published handling.
A footnote with code "P" marks a preliminary print (seen live on
CES0500000003's two most recent months) -- mapped to
`Observation.is_preliminary`.

Deliberately does NOT request a startyear/endyear range: BLS's unregistered
tier allows only a 10-year span per request and 25 requests/day total
(https://www.bls.gov/developers/api_faqs.htm), and paging back through a
CPI series' full multi-decade history would burn most of that quota on
every run. A bare request (no year range) returns roughly the trailing
2-3 years, which is enough for every incremental refetch -- older
observations already in `observations` (originally populated by the
DBnomics mirror before this migration, or by an earlier BLS backfill)
are untouched, since `indicators/fetch.py._store_observations` only
inserts/updates the period_start rows a fetch actually returns. If a
BLS_API_KEY is present (free registration, raises the daily quota to 500
requests and the per-request span to 20 years -- see .env.example), a
20-year window is requested instead, since the higher quota can absorb it
and it meaningfully improves first-time backfill coverage.
"""

from __future__ import annotations

import calendar
import os
from datetime import date, datetime, timezone

import httpx

from indicators import http
from indicators.config import get_config, resolve_path
from indicators.providers import Observation

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/{series_id}"

# Registered-key window: BLS's documented registered-tier max span.
REGISTERED_YEAR_SPAN = 20


def _load_dotenv() -> None:
    """Minimal .env loader, mirrors pipeline/cerebras.py's -- so
    BLS_API_KEY works locally without adding a dependency. GitHub Actions
    supplies the same var via a repo secret in CI, where no .env file
    exists, so this is a no-op there."""
    env_path = resolve_path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _get_api_key() -> str | None:
    _load_dotenv()
    return os.environ.get("BLS_API_KEY") or None


def _is_preliminary(row: dict) -> bool:
    return any(note.get("code") == "P" for note in row.get("footnotes", []) if note)


def _period_bounds(row: dict) -> tuple[date, date] | None:
    period = row.get("period", "")
    if not period.startswith("M") or period == "M13":
        return None  # M13 is BLS's annual-average pseudo-period, not a month
    try:
        year = int(row["year"])
        month = int(period[1:])
    except (KeyError, ValueError):
        return None
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches recent observations for one series from the BLS public API.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` is used.
    """
    cfg = get_config()
    timeout = cfg["indicators"].get("fetch_timeout_seconds", 20)

    series_id = series_spec["provider_series_id"]
    url = BLS_API_URL.format(series_id=series_id)

    params: dict[str, str] = {}
    api_key = _get_api_key()
    if api_key:
        current_year = datetime.now(timezone.utc).year
        params = {
            "registrationkey": api_key,
            "startyear": str(current_year - REGISTERED_YEAR_SPAN + 1),
            "endyear": str(current_year),
        }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await http.get(client, url, params=params)
        payload = resp.json()

    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError(f"BLS API error for {series_id!r}: {payload.get('message')}")

    series_list = payload.get("Results", {}).get("series") or []
    rows = series_list[0].get("data", []) if series_list else []

    observations: list[Observation] = []
    for row in rows:
        bounds = _period_bounds(row)
        if bounds is None:
            continue
        try:
            value = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        period_start, period_end = bounds
        observations.append(
            Observation(
                period_start=period_start,
                period_end=period_end,
                value=value,
                is_preliminary=_is_preliminary(row),
            )
        )
    return observations
