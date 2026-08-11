"""UK Office for National Statistics API provider (https://api.ons.gov.uk/v1/data).

`series.provider_series_id` is the ONS website's own URI path for the
series, e.g. "economy/inflationandpriceindices/timeseries/dko8/mm23" for
CPI ex-energy/food/alcohol/tobacco (CDID DKO8, dataset MM23) -- confirmed
live 2026-08-10 by testing candidate CDIDs against
https://api.ons.gov.uk/v1/data?uri=/economy/inflationandpriceindices/timeseries/dko8/mm23.
Using the full URI (rather than just "<dataset>/<cdid>", which this
module's config/indicators/series/*.yaml entry originally used) is
deliberate: unlike DBnomics/ECB/Eurostat, the ONS API has no single
dataset-agnostic resource path -- the topic segment
("economy/inflationandpriceindices/...") varies by series and there is no
documented API route that resolves a bare CDID without it, so the
provider_series_id has to carry the full path.

Response shape (confirmed live 2026-08-10, trimmed copy saved as
tests/fixtures/ons_cpi_core.json): {"months": [{"date": "2026 JUN",
"value": "142.5", ...}, ...]} for monthly series (there are parallel
"years"/"quarters" arrays for annual/quarterly series -- see
_OBSERVATIONS_KEY_BY_FREQUENCY below). "value" is a string; a small
fraction of ONS series use non-numeric placeholders like "" or ".." for a
not-yet-published period, which float() would reject, so those rows are
skipped rather than raising (this is a live data feed, not a fixed
historical archive -- a trailing not-yet-published period is expected,
not an error).

No revision/preliminary flag is exposed here either (matches
dbnomics.py/ecb.py/eurostat.py); observation_revisions isn't wired up yet.
"""

from __future__ import annotations

import calendar
from datetime import date

import httpx

from indicators.config import get_config
from indicators.providers import Observation

_OBSERVATIONS_KEY_BY_FREQUENCY = {
    "A": "years",
    "Q": "quarters",
    "M": "months",
}

_QUARTER_END_MONTH = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}


def _period_bounds(row: dict, frequency: str) -> tuple[date, date] | None:
    """Returns (period_start, period_end) for one months/quarters/years row,
    or None if the row's own date fields don't parse (defensive against a
    malformed or unexpected row rather than crashing the whole fetch).
    """
    year = int(row["year"])
    if frequency == "A":
        return date(year, 1, 1), date(year, 12, 31)
    if frequency == "Q":
        quarter = row["quarter"]  # e.g. "Q2"
        end_month = _QUARTER_END_MONTH.get(quarter)
        if end_month is None:
            return None
        start_month = end_month - 2
        return date(year, start_month, 1), date(year, end_month, calendar.monthrange(year, end_month)[1])
    # Monthly: row["month"] is a full month name, e.g. "June".
    month = list(calendar.month_name).index(row["month"]) if row.get("month") else None
    if not month:
        return None
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches every available observation for one series from the ONS API.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` and `frequency` are used.
    """
    cfg = get_config()
    indicators_cfg = cfg["indicators"]
    base_url = indicators_cfg["ons_base_url"].rstrip("/")
    timeout = indicators_cfg.get("fetch_timeout_seconds", 20)

    uri = series_spec["provider_series_id"].lstrip("/")

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(base_url, params={"uri": f"/{uri}"}, follow_redirects=True)
        resp.raise_for_status()
        payload = resp.json()

    frequency = series_spec.get("frequency") or "M"
    rows_key = _OBSERVATIONS_KEY_BY_FREQUENCY.get(frequency, "months")
    rows = payload.get(rows_key) or []

    observations: list[Observation] = []
    for row in rows:
        bounds = _period_bounds(row, frequency)
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
                is_preliminary=False,
            )
        )
    return observations
