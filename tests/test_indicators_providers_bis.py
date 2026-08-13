"""Tests indicators/providers/bis.py against a recorded fixture -- never a
live network call, mirrors tests/test_indicators_providers_boe.py's pattern.

tests/fixtures/bis_house_prices_us.csv is a trimmed copy of a real
response, fetched live 2026-08-13 from
https://stats.bis.org/api/v2/data/dataflow/BIS/WS_SPP/1.0/Q.US.R.628?format=csv.
"""

import asyncio
from datetime import date
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import bis

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "bis_house_prices_us.csv"


def _load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


_RealAsyncClient = httpx.AsyncClient


def _install_mock_client(monkeypatch, text: str, captured_requests: list, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code, text=text)

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(bis.httpx, "AsyncClient", factory)


def _base_config():
    return {
        "indicators": {
            "bis_base_url": "https://stats.bis.org/api/v2/data/dataflow",
            "fetch_timeout_seconds": 20,
        }
    }


def test_fetch_parses_quarterly_observations(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_requests: list = []
    _install_mock_client(monkeypatch, _load_fixture(), captured_requests)

    series_spec = {"series_id": "us.house_prices", "provider_series_id": "BIS/WS_SPP/Q.US.R.628"}
    observations = asyncio.run(bis.fetch(series_spec))

    assert len(observations) == 3
    assert all(isinstance(o, Observation) for o in observations)
    assert all(o.is_preliminary is False for o in observations)

    latest = next(o for o in observations if o.period_start == date(2026, 1, 1))
    assert latest.period_end.isoformat() == "2026-03-31"
    assert latest.value == 156.4275

    q3 = next(o for o in observations if o.period_start == date(2025, 7, 1))
    assert q3.period_end.isoformat() == "2025-09-30"
    assert q3.value == 159.7398

    assert len(captured_requests) == 1
    request_url = str(captured_requests[0].url)
    assert "/BIS/WS_SPP/1.0/Q.US.R.628" in request_url
    assert "format=csv" in request_url


def test_fetch_returns_empty_list_when_no_rows(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    header = "FREQ,REF_AREA,VALUE,UNIT_MEASURE,UNIT_MULT,BREAKS,COVERAGE,TITLE_TS,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK\n"
    _install_mock_client(monkeypatch, header, [])

    series_spec = {"series_id": "us.house_prices", "provider_series_id": "BIS/WS_SPP/Q.US.R.628"}
    observations = asyncio.run(bis.fetch(series_spec))

    assert observations == []


def test_split_series_id_rejects_malformed_id():
    try:
        bis._split_series_id("BIS/WS_SPP")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_quarter_bounds():
    assert bis._quarter_bounds("2026-Q1") == (date(2026, 1, 1), date(2026, 3, 31))
    assert bis._quarter_bounds("2025-Q4") == (date(2025, 10, 1), date(2025, 12, 31))
    assert bis._quarter_bounds("not-a-period") is None
