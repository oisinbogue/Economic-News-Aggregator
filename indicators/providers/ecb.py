"""ECB Data Portal SDMX-JSON provider (https://data-api.ecb.europa.eu/service/).

`series.provider_series_id` follows "<AGENCY>/<FLOW_REF>/<SDMX_KEY>", e.g.
"ECB/FM/D.U2.EUR.4F.KR.DFR.LEV" -- AGENCY is always the literal "ECB" in
config/indicators/series/*.yaml (kept for readability/consistency with the
"ECB/EXR/..." style already used there); only FLOW_REF and SDMX_KEY are used
to build the URL, .../service/data/<FLOW_REF>/<SDMX_KEY>.

Response shape (confirmed live 2026-08-10 against
.../service/data/FM/D.U2.EUR.4F.KR.DFR.LEV?format=jsondata, trimmed copy
saved as tests/fixtures/ecb_policy_rate.json): SDMX-JSON --
{"dataSets": [{"series": {"<series_key>": {"observations": {"<obs_index>":
[value, ...], ...}}}}], "structure": {"dimensions": {"observation": [{"id":
"TIME_PERIOD", "values": [{"id": "2026-08-06"}, ...]}]}}}. A fully-specified
SDMX key (no wildcards, as all of ours are) always resolves to exactly one
entry under dataSets[0]["series"] -- observations map an index (as a string)
into structure.dimensions.observation[0].values, and each observation's
value is index 0 of its array (the rest are attribute indices, unused here).

TIME_PERIOD ids vary in granularity by series frequency: "YYYY-MM-DD"
(daily), "YYYY-MM" (monthly), "YYYY-Qn" (quarterly), "YYYY" (annual) --
_parse_time_period() below handles all four regardless of what
series_spec["frequency"] claims, since the ECB's own id format is the
ground truth for how to compute period_start.

There is no revision/preliminary flag in this response, so
`Observation.is_preliminary` is always False here, matching dbnomics.py's
rationale (see that module's docstring) -- observation_revisions isn't
wired up yet.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from indicators import http
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
    "W": lambda start: start + timedelta(days=6),
    "D": lambda start: start,
}


def _parse_time_period(time_id: str) -> date:
    """Parses an ECB TIME_PERIOD id into its period_start date."""
    if "-Q" in time_id:
        year_str, quarter_str = time_id.split("-Q")
        return _add_months(date(int(year_str), 1, 1), (int(quarter_str) - 1) * 3)
    if time_id.count("-") == 2:
        return date.fromisoformat(time_id)
    if time_id.count("-") == 1:
        year_str, month_str = time_id.split("-")
        return date(int(year_str), int(month_str), 1)
    return date(int(time_id), 1, 1)


def _period_end(period_start: date, frequency: str) -> date:
    compute = _FREQUENCY_PERIOD_END.get(frequency)
    return compute(period_start) if compute else period_start


def _split_series_id(provider_series_id: str) -> tuple[str, str]:
    """Splits "<AGENCY>/<FLOW_REF>/<SDMX_KEY>" into (flow_ref, sdmx_key)."""
    parts = provider_series_id.split("/", 2)
    if len(parts) != 3:
        raise ValueError(f"malformed provider_series_id: {provider_series_id!r}")
    _agency, flow_ref, sdmx_key = parts
    return flow_ref, sdmx_key


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches every available observation for one series from the ECB Data Portal.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` and `frequency` are used.
    """
    cfg = get_config()
    indicators_cfg = cfg["indicators"]
    base_url = indicators_cfg["ecb_base_url"].rstrip("/")
    timeout = indicators_cfg.get("fetch_timeout_seconds", 20)

    flow_ref, sdmx_key = _split_series_id(series_spec["provider_series_id"])
    url = f"{base_url}/{flow_ref}/{sdmx_key}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await http.get(client, url, params={"format": "jsondata"}, follow_redirects=True)
        payload = resp.json()

    data_sets = payload.get("dataSets")
    if not data_sets or not data_sets[0].get("series"):
        return []

    series_data = next(iter(data_sets[0]["series"].values()))
    observations_by_index = series_data.get("observations", {})
    if not observations_by_index:
        return []

    time_values = payload["structure"]["dimensions"]["observation"][0]["values"]
    frequency = series_spec.get("frequency") or "M"

    observations: list[Observation] = []
    for index_str, obs in observations_by_index.items():
        raw_value = obs[0] if obs else None
        if raw_value is None:
            continue
        period_start = _parse_time_period(time_values[int(index_str)]["id"])
        observations.append(
            Observation(
                period_start=period_start,
                period_end=_period_end(period_start, frequency),
                value=float(raw_value),
                is_preliminary=False,
            )
        )
    return observations
