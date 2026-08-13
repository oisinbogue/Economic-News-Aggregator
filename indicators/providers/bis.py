"""Bank for International Settlements Statistics API provider
(https://stats.bis.org/api/v2/data/dataflow/).

Migrated off DBnomics' `BIS/WS_SPP` mirror 2026-08-13: that mirror had
stopped tracking upstream (frozen at Q1 2025, 500-590 days stale by the
time this was investigated) while BIS's own API is current -- confirmed
live against `Q.US.R.628`, `Q.GB.R.628`, `Q.IE.R.628`, `Q.DE.R.628` (the
four house_prices series active in Phase-1 config), all returning
observations through 2026-Q1.

`series.provider_series_id` keeps the exact same "BIS/<dataset_code>/<key>"
string the old DBnomics mapping used (e.g. "BIS/WS_SPP/Q.US.R.628" --
DBnomics' own BIS mirror uses BIS's native dataset/key naming unchanged),
so migrating a series is a config-only `provider: dbnomics` ->
`provider: bis` change, no provider_series_id edit needed.

Response shape (format=csv, confirmed live 2026-08-13): a flat CSV with one
row per observation --
`FREQ,REF_AREA,VALUE,UNIT_MEASURE,UNIT_MULT,BREAKS,COVERAGE,TITLE_TS,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK`.
`TIME_PERIOD` is "YYYY-Qn" for this (quarterly) dataflow -- no other
frequency is used by any BIS-provided series in config/indicators/series/
today, so only quarterly parsing is implemented.

No revision/preliminary flag is meaningfully exposed -- every row observed
live carries `OBS_STATUS=A` ("normal") -- so `Observation.is_preliminary`
is always False here, matching dbnomics.py/ecb.py/eurostat.py.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import httpx

from indicators import http
from indicators.config import get_config
from indicators.providers import Observation


def _quarter_bounds(time_period: str) -> tuple[date, date] | None:
    """Parses a "YYYY-Qn" TIME_PERIOD into (period_start, period_end)."""
    if "-Q" not in time_period:
        return None
    year_str, quarter_str = time_period.split("-Q")
    try:
        year = int(year_str)
        quarter = int(quarter_str)
    except ValueError:
        return None
    if not 1 <= quarter <= 4:
        return None
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    if end_month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, end_month + 1, 1) - timedelta(days=1)
    return date(year, start_month, 1), end


def _split_series_id(provider_series_id: str) -> tuple[str, str]:
    """Splits "BIS/<dataset_code>/<key>" into (dataset_code, key)."""
    parts = provider_series_id.split("/", 2)
    if len(parts) != 3:
        raise ValueError(f"malformed provider_series_id: {provider_series_id!r}")
    _prefix, dataset_code, key = parts
    return dataset_code, key


def _parse_csv(csv_text: str) -> list[Observation]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or "TIME_PERIOD" not in reader.fieldnames or "OBS_VALUE" not in reader.fieldnames:
        return []

    observations: list[Observation] = []
    for row in reader:
        bounds = _quarter_bounds(row.get("TIME_PERIOD", ""))
        if bounds is None:
            continue
        raw_value = row.get("OBS_VALUE")
        if not raw_value:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        period_start, period_end = bounds
        observations.append(
            Observation(period_start=period_start, period_end=period_end, value=value, is_preliminary=False)
        )
    return observations


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches every available observation for one series from the BIS
    Statistics API.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` is used.
    """
    cfg = get_config()
    indicators_cfg = cfg["indicators"]
    base_url = indicators_cfg["bis_base_url"].rstrip("/")
    timeout = indicators_cfg.get("fetch_timeout_seconds", 20)

    dataset_code, key = _split_series_id(series_spec["provider_series_id"])
    url = f"{base_url}/BIS/{dataset_code}/1.0/{key}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await http.get(client, url, params={"format": "csv"}, follow_redirects=True)

    return _parse_csv(resp.text)
