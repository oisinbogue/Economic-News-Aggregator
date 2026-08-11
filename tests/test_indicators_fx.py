"""Tests indicators/fx.py's conversion logic and XML/CSV parsers.

Conversion tests use an in-memory sqlite db with just the `fx_rates` table
(fx.convert() only needs that one table, so this doesn't need the full
indicators schema or config fixtures the way test_indicators_db.py does).
"""

import sqlite3

import pytest

from indicators import fx


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE fx_rates (
            date TEXT NOT NULL,
            base TEXT NOT NULL,
            quote TEXT NOT NULL,
            rate REAL NOT NULL,
            PRIMARY KEY (date, base, quote)
        )
        """
    )
    yield connection
    connection.close()


def _seed(conn, rows):
    conn.executemany("INSERT INTO fx_rates (date, base, quote, rate) VALUES (?, ?, ?, ?)", rows)
    conn.commit()


# --------------------------------------------------------------------------
# convert()
# --------------------------------------------------------------------------

def test_convert_same_currency_is_a_noop(conn):
    import datetime
    result = fx.convert(100.0, "USD", "USD", datetime.date(2026, 1, 1), conn)
    assert result == 100.0


def test_convert_direct_eur_pair(conn):
    import datetime
    _seed(conn, [("2026-01-15", "EUR", "USD", 1.10)])

    # EUR -> USD: multiply by rate.
    assert fx.convert(100.0, "EUR", "USD", datetime.date(2026, 1, 15), conn) == pytest.approx(110.0)

    # USD -> EUR: divide by rate.
    assert fx.convert(110.0, "USD", "EUR", datetime.date(2026, 1, 15), conn) == pytest.approx(100.0)


def test_convert_cross_rate_via_eur_between_two_non_eur_currencies(conn):
    import datetime
    _seed(conn, [
        ("2026-01-15", "EUR", "USD", 1.10),
        ("2026-01-15", "EUR", "GBP", 0.85),
    ])

    # 110 USD -> EUR (100) -> GBP (85).
    result = fx.convert(110.0, "USD", "GBP", datetime.date(2026, 1, 15), conn)
    assert result == pytest.approx(85.0)


def test_convert_missing_rate_for_date_returns_none_not_raises(conn):
    import datetime
    _seed(conn, [("2026-01-15", "EUR", "USD", 1.10)])

    result = fx.convert(100.0, "EUR", "USD", datetime.date(2026, 1, 16), conn)
    assert result is None


def test_convert_missing_one_leg_of_a_cross_rate_returns_none(conn):
    import datetime
    _seed(conn, [("2026-01-15", "EUR", "USD", 1.10)])  # no GBP rate seeded

    result = fx.convert(110.0, "USD", "GBP", datetime.date(2026, 1, 15), conn)
    assert result is None


def test_convert_uses_period_matched_rate_not_most_recent_row(conn):
    """Regression test for the brief's single most important correctness
    rule in this module: convert() must use the rate for the observation's
    own period_date, never whatever the latest fx_rates row happens to be.
    """
    import datetime
    _seed(conn, [
        ("2020-01-15", "EUR", "USD", 1.50),   # old period's rate
        ("2026-08-07", "EUR", "USD", 1.10),   # most recently fetched rate
    ])

    old_period_result = fx.convert(100.0, "EUR", "USD", datetime.date(2020, 1, 15), conn)
    assert old_period_result == pytest.approx(150.0)
    # If convert() were (bugged into) using the most recent row instead of
    # the period-matched one, this would come back 110.0 -- guard against that.
    assert old_period_result != pytest.approx(110.0)


# --------------------------------------------------------------------------
# ECB XML parsing
# --------------------------------------------------------------------------

def test_parse_ecb_xml_daily_shape():
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
    <gesmes:subject>Reference rates</gesmes:subject>
    <gesmes:Sender><gesmes:name>European Central Bank</gesmes:name></gesmes:Sender>
    <Cube>
        <Cube time="2026-08-07">
            <Cube currency="USD" rate="1.1535"/>
            <Cube currency="GBP" rate="0.85765"/>
        </Cube>
    </Cube>
</gesmes:Envelope>"""

    rows = fx._parse_ecb_xml(xml_text)

    assert set(rows) == {
        ("2026-08-07", "EUR", "USD", 1.1535),
        ("2026-08-07", "EUR", "GBP", 0.85765),
    }


def test_parse_ecb_xml_hist_shape_multiple_dates():
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
    <Cube>
        <Cube time="2026-08-07"><Cube currency="USD" rate="1.1535"/></Cube>
        <Cube time="2026-08-06"><Cube currency="USD" rate="1.1542"/></Cube>
    </Cube>
</gesmes:Envelope>"""

    rows = fx._parse_ecb_xml(xml_text)

    assert set(rows) == {
        ("2026-08-07", "EUR", "USD", 1.1535),
        ("2026-08-06", "EUR", "USD", 1.1542),
    }


# --------------------------------------------------------------------------
# Stooq CSV parsing
# --------------------------------------------------------------------------

def test_parse_stooq_csv_returns_latest_close():
    csv_text = "Date,Open,High,Low,Close\n2026-08-05,1.10,1.11,1.09,1.105\n2026-08-06,1.105,1.12,1.10,1.115\n"
    result = fx._parse_stooq_csv(csv_text, "usd")
    assert result == ("2026-08-06", "EUR", "USD", 1.115)


def test_parse_stooq_csv_returns_none_for_non_csv_response():
    # Stooq's JS bot-check challenge page, not CSV -- confirmed live 2026-08-10.
    html_text = '<!DOCTYPE html><html><head></head><body>challenge</body></html>'
    assert fx._parse_stooq_csv(html_text, "usd") is None


def test_parse_stooq_csv_returns_none_when_no_usable_rows():
    csv_text = "Date,Open,High,Low,Close\n2026-08-06,1.10,1.11,1.09,N/D\n"
    assert fx._parse_stooq_csv(csv_text, "usd") is None
