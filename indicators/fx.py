"""EUR-based FX rates for the indicators subsystem.

Populates and reads `fx_rates` (INDICATORS_SCHEMA_PROPOSAL.md §3: `date,
base, quote, rate`, base always 'EUR' for the ECB feed). Two fetchers plus
one conversion function:

  - `fetch_ecb_rates()` -- the ECB's free eurofxref reference-rate feed
    (no key required). Response shape confirmed live 2026-08-10 against
    https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml:
    XML, `<Cube><Cube time="YYYY-MM-DD"><Cube currency="USD"
    rate="1.1535"/>...</Cube></Cube>`, base implicitly EUR (that's all the
    feed publishes), `rate` = units of `currency` per 1 EUR. The classic
    eurofxref feed is XML only -- there is no CSV variant at this path,
    despite some older docs suggesting one.
  - `fetch_stooq_rate()` -- fallback for a currency the ECB feed doesn't
    cover (ECB only publishes ~30 major/EU currencies). Historically a
    plain CSV at stooq.com/q/d/l/; confirmed live 2026-08-10 that Stooq
    now gates this endpoint behind a JS proof-of-work bot check and
    returns an HTML challenge page to a plain HTTP client instead of CSV.
    `_parse_stooq_csv()` is written defensively against that: a response
    that isn't actually CSV (missing the Date/Close header) is treated as
    "no data", same as an empty result, rather than raised -- this
    fallback was never guaranteed to have every pair anyway.
  - `convert()` -- period-matched conversion using whatever is already in
    `fx_rates` (does not fetch). This is the single most important
    correctness rule in this module: an observation for period P must be
    converted using the FX rate *for P*, never the latest rate in the
    table -- see the function docstring.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from xml.etree import ElementTree as ET

import httpx

from indicators.config import get_config

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_HIST_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
STOOQ_CSV_URL = "https://stooq.com/q/d/l/"

_ECB_CUBE_TAG = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}Cube"

INSERT_FX_RATE_SQL = "INSERT OR IGNORE INTO fx_rates (date, base, quote, rate) VALUES (?, ?, ?, ?)"


# --------------------------------------------------------------------------
# ECB
# --------------------------------------------------------------------------

def _parse_ecb_xml(xml_text: str) -> list[tuple[str, str, str, float]]:
    """Parses an ECB eurofxref XML payload into (date, 'EUR', quote, rate) rows.

    Works for both the daily feed (one <Cube time=...> element) and the
    hist feeds (one per available date) -- only the middle-level <Cube
    time=...> elements carry a `time` attribute, so filtering on that
    distinguishes them from the outer wrapper <Cube> and the innermost
    per-currency <Cube currency= rate=/> elements without needing to track
    nesting depth explicitly.
    """
    root = ET.fromstring(xml_text)
    rows: list[tuple[str, str, str, float]] = []
    for time_cube in root.iter(_ECB_CUBE_TAG):
        time_attr = time_cube.get("time")
        if time_attr is None:
            continue
        for rate_cube in time_cube:
            currency = rate_cube.get("currency")
            rate = rate_cube.get("rate")
            if currency is None or rate is None:
                continue
            rows.append((time_attr, "EUR", currency, float(rate)))
    return rows


async def fetch_ecb_rates(hist: bool = True) -> list[tuple[str, str, str, float]]:
    """Fetches ECB reference rates: the last ~90 days by default (`hist=True`,
    enough to cover a normal fetch.py run's cadence), or just today's rates
    (`hist=False`, e.g. for a quick manual check).
    """
    cfg = get_config()
    timeout = cfg.get("indicators", {}).get("fetch_timeout_seconds", 20)
    url = ECB_HIST_90D_URL if hist else ECB_DAILY_URL

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()

    return _parse_ecb_xml(resp.text)


# --------------------------------------------------------------------------
# Stooq (fallback for pairs the ECB feed doesn't publish)
# --------------------------------------------------------------------------

def _parse_stooq_csv(csv_text: str, quote: str) -> tuple[str, str, str, float] | None:
    """Parses a Stooq daily-history CSV (`Date,Open,High,Low,Close`) into
    the latest (date, 'EUR', quote, close) row. Returns None if the
    response isn't actually that CSV shape (e.g. Stooq's bot-check HTML
    page, or an unknown symbol returning an empty body) or has no usable
    rows -- never raises, since this is a best-effort fallback.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or "Date" not in reader.fieldnames or "Close" not in reader.fieldnames:
        return None

    rows = [r for r in reader if r.get("Date") and r.get("Close") not in (None, "", "N/D")]
    if not rows:
        return None

    last = rows[-1]
    try:
        return (last["Date"], "EUR", quote.upper(), float(last["Close"]))
    except ValueError:
        return None


async def fetch_stooq_rate(quote: str) -> tuple[str, str, str, float] | None:
    """Fetches the latest EUR-per-`quote` rate from Stooq's daily-history
    CSV (symbol "eur<quote>", e.g. "eurtry" -- Stooq's own convention
    quotes the second currency's units per one of the first, matching the
    base=EUR/rate=quote-per-EUR convention `fx_rates` already uses for the
    ECB rows). Returns None (not raises) if Stooq has no data for the pair.
    """
    cfg = get_config()
    timeout = cfg.get("indicators", {}).get("fetch_timeout_seconds", 20)
    symbol = f"eur{quote.lower()}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(STOOQ_CSV_URL, params={"s": symbol, "i": "d"}, follow_redirects=True)
        resp.raise_for_status()

    return _parse_stooq_csv(resp.text, quote)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def store_fx_rates(conn, rows: list[tuple[str, str, str, float]]) -> int:
    """Inserts (date, base, quote, rate) rows into `fx_rates`, ignoring any
    (date, base, quote) already stored -- a reference rate for a given day
    never changes, so there's nothing to reconcile on a repeat fetch.
    Returns the number of new rows actually written.
    """
    before = conn.total_changes
    conn.executemany(INSERT_FX_RATE_SQL, rows)
    return conn.total_changes - before


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------

def _rate_for(conn, date_str: str, base: str, quote: str) -> float | None:
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE date = ? AND base = ? AND quote = ?",
        (date_str, base, quote),
    ).fetchone()
    return row["rate"] if row is not None else None


def convert(amount: float, from_currency: str, to_currency: str, period_date: date, conn) -> float | None:
    """Converts `amount` from `from_currency` to `to_currency` using the FX
    rate for `period_date` -- the observation's own reference period --
    never today's rate or whatever the most recently fetched fx_rates row
    happens to be. Using any other date's rate would silently mix, e.g., a
    January GDP figure with a rate fetched the day the pipeline last ran,
    which is exactly the bug this function exists to prevent.

    Crosses via EUR (`fx_rates.base` is always 'EUR') when neither currency
    is EUR: amount -> EUR using 1/rate(EUR->from), then EUR -> to using
    rate(EUR->to).

    Returns None -- never raises -- when no rate exists for that date and
    pair, so callers (fetch.py) can leave `value_usd` null for that
    observation instead of crashing the whole fetch run.
    """
    if from_currency == to_currency:
        return amount

    date_str = period_date.isoformat()

    if from_currency == "EUR":
        rate = _rate_for(conn, date_str, "EUR", to_currency)
        return amount * rate if rate is not None else None

    if to_currency == "EUR":
        rate = _rate_for(conn, date_str, "EUR", from_currency)
        return amount / rate if rate is not None else None

    eur_per_from = _rate_for(conn, date_str, "EUR", from_currency)
    eur_per_to = _rate_for(conn, date_str, "EUR", to_currency)
    if eur_per_from is None or eur_per_to is None:
        return None

    amount_in_eur = amount / eur_per_from
    return amount_in_eur * eur_per_to
