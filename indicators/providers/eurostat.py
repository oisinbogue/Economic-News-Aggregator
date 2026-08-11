"""Eurostat SDMX dissemination API provider
(https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/).

`series.provider_series_id` follows "Eurostat/<dataset_code>/<dot_key>",
e.g. "Eurostat/ei_bsco_m/M.BS-CSMCI.SA.BAL.EA20" -- <dot_key> is a
dot-separated positional key over the dataset's own dimension order (which
differs per dataset; there is no fixed freq/geo/unit convention across
Eurostat datasets the way there is for a single ECB dataflow -- each of
this module's config/indicators/series/*.yaml entries was confirmed live
2026-08-10 against its own dataset's dimension order, one query at a time,
by trial substitution against the "not allowed" 400 error's per-dimension
detail).

Response shape (format=JSON, confirmed live 2026-08-10; trimmed copies
saved as tests/fixtures/eurostat_*.json): despite the `format=JSON` query
param name suggesting SDMX-JSON, Eurostat's dissemination API actually
returns JSON-stat 2.0 -- {"value": {"<flat_index>": value, ...},
"dimension": {"time": {"category": {"index": {"<time_label>":
<flat_index>, ...}}}, ...}}. Since every provider_series_id here is a
fully-specified key (no wildcards), every non-time dimension has exactly
one category, so <flat_index> in `value` is always identical to the
time dimension's own index for that period -- no need to compute the
general multi-dimensional row-major offset.

<time_label> format depends on the series' frequency: "YYYY-MM" (monthly),
"YYYY-Qn" (quarterly), "YYYY" (annual) -- Eurostat has no daily datasets
among config/indicators/series/*.yaml's Eurostat-provided series.

No revision/preliminary flag is exposed, so `Observation.is_preliminary`
is always False here, same as dbnomics.py/ecb.py.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from indicators.config import get_config
from indicators.providers import Observation


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


_FREQUENCY_PERIOD_END = {
    "A": lambda start: date(start.year + 1, 1, 1) - timedelta(days=1),
    "Q": lambda start: _add_months(start, 3) - timedelta(days=1),
    "M": lambda start: _add_months(start, 1) - timedelta(days=1),
}


def _period_end(period_start: date, frequency: str) -> date:
    compute = _FREQUENCY_PERIOD_END.get(frequency)
    return compute(period_start) if compute else period_start


def _parse_time_label(time_label: str) -> date:
    """Parses a JSON-stat time-dimension label into its period_start date."""
    if "-Q" in time_label:
        year_str, quarter_str = time_label.split("-Q")
        return _add_months(date(int(year_str), 1, 1), (int(quarter_str) - 1) * 3)
    if "-" in time_label:
        year_str, month_str = time_label.split("-")
        return date(int(year_str), int(month_str), 1)
    return date(int(time_label), 1, 1)


def _split_series_id(provider_series_id: str) -> tuple[str, str]:
    """Splits "Eurostat/<dataset_code>/<dot_key>" into (dataset_code, dot_key)."""
    parts = provider_series_id.split("/", 2)
    if len(parts) != 3:
        raise ValueError(f"malformed provider_series_id: {provider_series_id!r}")
    _prefix, dataset_code, dot_key = parts
    return dataset_code, dot_key


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches every available observation for one series from Eurostat.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` and `frequency` are used.
    """
    cfg = get_config()
    indicators_cfg = cfg["indicators"]
    base_url = indicators_cfg["eurostat_base_url"].rstrip("/")
    timeout = indicators_cfg.get("fetch_timeout_seconds", 20)

    dataset_code, dot_key = _split_series_id(series_spec["provider_series_id"])
    url = f"{base_url}/{dataset_code}/{dot_key}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params={"format": "JSON"}, follow_redirects=True)
        resp.raise_for_status()
        payload = resp.json()

    if "error" in payload:
        detail = payload["error"]
        raise ValueError(f"Eurostat error for {series_spec['provider_series_id']!r}: {detail}")

    values_by_index = payload.get("value") or {}
    if not values_by_index:
        return []

    time_index = payload["dimension"]["time"]["category"]["index"]
    index_to_label = {index: label for label, index in time_index.items()}
    frequency = series_spec.get("frequency") or "M"

    observations: list[Observation] = []
    for index_str, raw_value in values_by_index.items():
        if raw_value is None:
            continue
        time_label = index_to_label[int(index_str)]
        period_start = _parse_time_label(time_label)
        observations.append(
            Observation(
                period_start=period_start,
                period_end=_period_end(period_start, frequency),
                value=float(raw_value),
                is_preliminary=False,
            )
        )
    return observations
