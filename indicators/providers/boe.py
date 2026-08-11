"""Bank of England Interactive Statistical Database (IADB) provider
(https://www.bankofengland.co.uk/boeapps/iadb/).

`series.provider_series_id` is a bare IADB series code, e.g. "IUMBEDR" for
Bank Rate -- confirmed live 2026-08-10 against
.../fromshowcolumns.asp?csv.x=yes&Datefrom=01/Jan/1975&Dateto=01/Jan/2027&SeriesCodes=IUMBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N,
which returns a two-column CSV `DATE,<SERIES_CODE>` with dates formatted
"DD Mon YYYY" (e.g. "31 Jul 2026").

`Dateto` is capped at *today*, not some far-future placeholder: querying
with a Dateto beyond the series' last real update was observed live to
return extra rows that repeat the last known value forward through the
requested end date (an IADB quirk, not real future data) -- fetching
those would silently fabricate observations for periods that haven't
happened yet.

The Bank Rate series (the only one this module currently backs, per
config/indicators/series/gb.yaml's `policy_rate`) is nominally daily
(`frequency: D` in core.yaml) but IADB only republishes a new CSV row when
the rate changes or at each month-end mark-to-market, not one row per
calendar day -- so `period_end` is always set equal to `period_start`
(the "D" case in dbnomics.py's `_FREQUENCY_PERIOD_END`) rather than
inferring a monthly span from the gaps between rows, since a gap here
means "unchanged", not "the observation was for a monthly interval".

No revision/preliminary flag is exposed, so `Observation.is_preliminary`
is always False here, matching this package's other provider modules.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

import httpx

from indicators import http
from indicators.config import get_config
from indicators.providers import Observation


def _parse_boe_csv(csv_text: str, series_code: str) -> list[tuple[date, float]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or "DATE" not in reader.fieldnames or series_code not in reader.fieldnames:
        return []

    rows: list[tuple[date, float]] = []
    for row in reader:
        raw_date = row.get("DATE")
        raw_value = row.get(series_code)
        if not raw_date or raw_value in (None, "", "ND"):
            continue
        try:
            period = datetime.strptime(raw_date, "%d %b %Y").date()
            value = float(raw_value)
        except ValueError:
            continue
        rows.append((period, value))
    return rows


async def fetch(series_spec: dict) -> list[Observation]:
    """Fetches every available observation for one series from the BoE IADB.

    `series_spec` is a `series` table row (dict-like); only
    `provider_series_id` is used (BoE IADB series are always daily-or-sparser
    level snapshots -- see module docstring).
    """
    cfg = get_config()
    indicators_cfg = cfg["indicators"]
    base_url = indicators_cfg["boe_base_url"]
    timeout = indicators_cfg.get("fetch_timeout_seconds", 20)

    series_code = series_spec["provider_series_id"]
    today = datetime.now(timezone.utc).date()

    params = {
        "csv.x": "yes",
        "Datefrom": "01/Jan/1975",
        "Dateto": today.strftime("%d/%b/%Y"),
        "SeriesCodes": series_code,
        "CSVF": "TN",
        "UsingCodes": "Y",
        "VPD": "Y",
        "VFD": "N",
    }

    # The BoE site's edge (Akamai) returns a hard 403 to any request without
    # a browser-like User-Agent -- confirmed live 2026-08-11, no other
    # headers or cookies needed once this is set.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await http.get(client, base_url, params=params, headers=headers, follow_redirects=True)

    rows = _parse_boe_csv(resp.text, series_code)
    return [
        Observation(period_start=period, period_end=period, value=value, is_preliminary=False)
        for period, value in rows
    ]
