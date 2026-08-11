"""Tests indicators/providers/boe.py against a recorded CSV fixture -- never
a live network call, mirrors tests/test_indicators_providers.py's pattern
(adapted for a CSV rather than JSON response body, like
tests/test_indicators_fx.py's Stooq CSV tests).

tests/fixtures/boe_bank_rate.csv is a trimmed copy of a real response,
fetched live 2026-08-10 from
https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp?csv.x=yes&Datefrom=01/Jan/1975&Dateto=01/Jan/2027&SeriesCodes=IUMBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N
(Bank Rate, series code IUMBEDR).
"""

import asyncio
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import boe

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "boe_bank_rate.csv"


def _load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


_RealAsyncClient = httpx.AsyncClient


def _install_mock_client(monkeypatch, body: str, captured_urls: list, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(status_code, text=body)

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(boe.httpx, "AsyncClient", factory)


def _base_config():
    return {
        "indicators": {
            "boe_base_url": "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp",
            "fetch_timeout_seconds": 20,
        }
    }


def test_fetch_parses_observations_from_fixture(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_urls: list = []
    _install_mock_client(monkeypatch, _load_fixture(), captured_urls)

    series_spec = {"series_id": "gb.policy_rate", "provider_series_id": "IUMBEDR", "frequency": "D"}
    observations = asyncio.run(boe.fetch(series_spec))

    assert len(observations) == 4
    assert all(isinstance(o, Observation) for o in observations)

    first = observations[0]
    assert first.period_start.isoformat() == "2026-04-30"
    assert first.period_end.isoformat() == "2026-04-30"
    assert first.value == 3.75
    assert first.is_preliminary is False

    assert len(captured_urls) == 1
    assert "SeriesCodes=IUMBEDR" in captured_urls[0]


def test_fetch_returns_empty_list_for_non_csv_response(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    _install_mock_client(monkeypatch, "<html>not csv</html>", [])

    series_spec = {"series_id": "gb.policy_rate", "provider_series_id": "IUMBEDR", "frequency": "D"}
    observations = asyncio.run(boe.fetch(series_spec))

    assert observations == []


def test_parse_boe_csv_skips_nd_rows():
    csv_text = "DATE,IUMBEDR\n31 Jan 2026,3.75\n28 Feb 2026,ND\n"
    rows = boe._parse_boe_csv(csv_text, "IUMBEDR")
    assert len(rows) == 1
    assert rows[0][1] == 3.75
