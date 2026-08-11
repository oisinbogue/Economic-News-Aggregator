"""DBnomics REST API provider (https://api.db.nomics.world/v22/).

`series.provider_series_id` follows either:
  - "<PROVIDER_CODE>/<SERIES_CODE>" (2 segments) -- shorthand for a
    single-series dataset, i.e. dataset_code == series_code. Resolved via
    DBnomics' dataset-listing endpoint (no series_code in the URL, just
    `limit=1`) since a single-series dataset has exactly one doc to return
    regardless of ordering.
  - "<PROVIDER_CODE>/<DATASET_CODE>/<SERIES_CODE>" (3+ segments) -- the
    general case, e.g. "ECB/EXR/D.USD.EUR.SP00.A". Everything between the
    first and last "/" is the dataset code.

Response shape (confirmed live 2026-08-10 against
.../v22/series/BEA/NIPA-T10105?observations=1&limit=100, trimmed copy
saved as tests/fixtures/dbnomics_gdpc1.json): a successful response is
{"errors": null, "series": {"docs": [{"period_start_day": [...], "value":
[...], ...}], ...}, ...}. Each doc's "period_start_day" and "value" arrays
are parallel; "value" can contain `null` for periods with no data yet.
There is no revision/preliminary flag in this response, so
`Observation.is_preliminary` is always False here -- DBnomics doesn't
expose the distinction the brief's `observation_revisions` table wants;
detecting "preliminary" would require diffing against the previous fetch,
which is exactly what observation_revisions is for (not wired up yet, see
indicators/fetch.py).

NOTE: DBnomics has no 'FRED' provider (only 'FED', the Federal Reserve
Board -- see .../v22/providers). config/indicators/series/*.yaml's
`provider_series_id: FRED/...` entries from an earlier session (invented
without checking against the live API) have been replaced with real
provider/dataset/series codes (BEA, BLS, FED, OECD, IMF, BIS, as
appropriate per series) -- confirmed live 2026-08-11.
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


def _period_end(period_start: date, frequency: str) -> date:
    compute = _FREQUENCY_PERIOD_END.get(frequency)
    return compute(period_start) if compute else period_start


def _split_series_id(provider_series_id: str) -> tuple[str, str, str | None]:
    """Splits a provider_series_id into (provider_code, dataset_code, series_code).

    series_code is None for the 2-segment single-series-dataset shorthand
    (see module docstring) -- callers omit it from the URL and rely on
    `limit=1` to fetch the dataset's one series.
    """
    parts = provider_series_id.split("/")
    if len(parts) < 2:
        raise ValueError(f"malformed provider_series_id: {provider_series_id!r}")
    if len(parts) == 2:
        return parts[0], parts[1], None
    return parts[0], parts[1], "/".join(parts[2:])


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches every available observation for one series from DBnomics.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` and `frequency` are used.
    """
    cfg = get_config()
    indicators_cfg = cfg["indicators"]
    base_url = indicators_cfg["dbnomics_base_url"].rstrip("/")
    timeout = indicators_cfg.get("fetch_timeout_seconds", 20)

    provider_code, dataset_code, series_code = _split_series_id(series_spec["provider_series_id"])
    url = f"{base_url}/series/{provider_code}/{dataset_code}"
    if series_code:
        url += f"/{series_code}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await http.get(client, url, params={"observations": 1, "limit": 1}, follow_redirects=True)
        payload = resp.json()

    if payload.get("errors") or "series" not in payload:
        detail = payload.get("errors") or payload.get("message")
        raise ValueError(f"DBnomics error for {series_spec['provider_series_id']!r}: {detail}")

    docs = payload["series"]["docs"]
    if not docs:
        return []
    doc = docs[0]

    frequency = series_spec.get("frequency") or "M"
    periods = doc.get("period_start_day") or []
    values = doc.get("value") or []

    observations: list[Observation] = []
    for period_str, raw_value in zip(periods, values):
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            # Some upstream providers (e.g. IMF WEO mirrored via DBnomics)
            # represent a missing observation as the string "NA" rather
            # than JSON null -- treat any non-numeric value the same as
            # None (skip it) instead of raising.
            continue
        period_start = date.fromisoformat(period_str)
        observations.append(
            Observation(
                period_start=period_start,
                period_end=_period_end(period_start, frequency),
                value=value,
                is_preliminary=False,
            )
        )
    return observations
