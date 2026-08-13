"""OECD SDMX Data API provider
(https://sdmx.oecd.org/public/rest/data/).

Migrated off DBnomics' `OECD/DSD_*@DF_*` mirrors 2026-08-13: those mirrors
were systemically 74-135 days behind across every country for these
short-term-economics/labour-force/earnings dataflows, while OECD's own API
is current -- confirmed live 2026-08-13 across every DSD_STES@DF_INDSERV,
DSD_STES@DF_FINMARK, DSD_STES@DF_CS, DSD_LFS@DF_IALFS_UNE_M and
DSD_EAR@DF_HOU_EAR series active in config/indicators/series/*.yaml,
returning observations through May/June 2026 (monthly) or Q1 2026
(quarterly).

`series.provider_series_id` keeps the exact same "OECD/<dataflow_id>/<key>"
string the old DBnomics mapping used (e.g.
"OECD/DSD_STES@DF_INDSERV/USA.M.TOVM.IX.G47.Y._Z._Z.N" -- DBnomics' own
OECD mirror uses OECD's native dataflow/key naming unchanged), so migrating
a series is a config-only `provider: dbnomics` -> `provider: oecd` change,
no provider_series_id edit needed. Note `<dataflow_id>` itself contains a
literal "@" (OECD's own "<DSD id>@<dataflow id>" convention, e.g.
"DSD_STES@DF_INDSERV") -- `_split_series_id` below splits on "/", not "@",
so that's transparent.

The request URL uses the literal agency id "all" rather than a specific
one (e.g. "OECD.SDD.STES") -- confirmed live 2026-08-13 that "all" resolves
every dataflow used here without needing a per-dataflow agency lookup
table, which would otherwise have to be maintained as OECD reorganises
dataflows between directorates.

sdmx.oecd.org's rate limiting is meaningfully stricter than every other
provider in this package -- confirmed live 2026-08-13: a burst of
~15-20 requests a few seconds apart started returning 429, and the block
outlasted indicators/http.py's own backoff window (~15s across its 4
retries), still 429ing on a single request sent 6s later, only clearing
after roughly a minute of no requests at all. config.yaml's
indicators.provider_concurrency/provider_delay_seconds for "oecd" are
deliberately the most conservative in the file (concurrency 1, 3s delay)
because of this -- see the comment there.

Response shape (format=jsondata, confirmed live 2026-08-13): SDMX-JSON 2.0,
shaped as {"data": {"structures": [{"dimensions": {"observation": [...,
{"id": "TIME_PERIOD", "values": [{"id": "2026-05"}, ...]}]}}],
"dataSets": [{"series": {"<series_key>": {"observations": {"<obs_index>":
[<value>, ...attributes]}}}}]}}. Since every provider_series_id here is a
fully-specified key (no wildcards), `dataSets[0].series` always has
exactly one entry, same assumption eurostat.py makes for JSON-stat.
`TIME_PERIOD` labels are "YYYY-MM" (monthly), "YYYY-Qn" (quarterly), or
"YYYY" (annual) -- mirrors eurostat.py's `_parse_time_label`.

No revision/preliminary flag is exposed, so `Observation.is_preliminary`
is always False here, matching this package's other provider modules.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from indicators import http
from indicators.config import get_config
from indicators.providers import Observation

OECD_AGENCY = "all"


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


def _parse_time_label(time_label: str) -> tuple[date, date] | None:
    """Parses an SDMX TIME_PERIOD label into (period_start, period_end)."""
    if "-Q" in time_label:
        year_str, quarter_str = time_label.split("-Q")
        start = _add_months(date(int(year_str), 1, 1), (int(quarter_str) - 1) * 3)
        return start, _FREQUENCY_PERIOD_END["Q"](start)
    if "-" in time_label:
        year_str, month_str = time_label.split("-")
        start = date(int(year_str), int(month_str), 1)
        return start, _FREQUENCY_PERIOD_END["M"](start)
    if time_label.isdigit():
        start = date(int(time_label), 1, 1)
        return start, _FREQUENCY_PERIOD_END["A"](start)
    return None


def _split_series_id(provider_series_id: str) -> tuple[str, str]:
    """Splits "OECD/<dataflow_id>/<key>" into (dataflow_id, key). Note
    `dataflow_id` itself may contain "@" (e.g. "DSD_STES@DF_INDSERV") --
    irrelevant here since the split is on "/", not "@"."""
    parts = provider_series_id.split("/", 2)
    if len(parts) != 3:
        raise ValueError(f"malformed provider_series_id: {provider_series_id!r}")
    _prefix, dataflow_id, key = parts
    return dataflow_id, key


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches every available observation for one series from the OECD
    SDMX Data API.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` is used.
    """
    cfg = get_config()
    indicators_cfg = cfg["indicators"]
    base_url = indicators_cfg["oecd_base_url"].rstrip("/")
    timeout = indicators_cfg.get("fetch_timeout_seconds", 20)

    dataflow_id, key = _split_series_id(series_spec["provider_series_id"])
    url = f"{base_url}/{OECD_AGENCY},{dataflow_id},/{key}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await http.get(client, url, params={"format": "jsondata"}, follow_redirects=True)
        payload = resp.json()

    structures = payload.get("data", {}).get("structures") or []
    data_sets = payload.get("data", {}).get("dataSets") or []
    if not structures or not data_sets:
        return []

    observation_dims = structures[0].get("dimensions", {}).get("observation") or []
    time_dim = next((d for d in observation_dims if d.get("id") == "TIME_PERIOD"), None)
    if time_dim is None:
        return []
    time_values = [v["id"] for v in time_dim["values"]]

    series_map = data_sets[0].get("series") or {}
    if not series_map:
        return []
    observations_by_index = next(iter(series_map.values())).get("observations") or {}

    observations: list[Observation] = []
    for index_str, obs_row in observations_by_index.items():
        if not obs_row or obs_row[0] is None:
            continue
        time_label = time_values[int(index_str)]
        bounds = _parse_time_label(time_label)
        if bounds is None:
            continue
        period_start, period_end = bounds
        observations.append(
            Observation(
                period_start=period_start,
                period_end=period_end,
                value=float(obs_row[0]),
                is_preliminary=False,
            )
        )
    return observations
