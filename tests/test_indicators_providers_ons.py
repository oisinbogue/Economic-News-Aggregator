"""Tests indicators/providers/ons.py against a recorded fixture -- never a
live network call, mirrors tests/test_indicators_providers.py's pattern.

tests/fixtures/ons_cpi_core.json is a trimmed copy of a real response,
fetched live 2026-08-10 from
https://api.ons.gov.uk/v1/data?uri=/economy/inflationandpriceindices/timeseries/dko8/mm23
(CDID DKO8: "CPI 12mth: Excluding Energy, food, alcoholic beverages &
tobacco"), with one month's value blanked to exercise the
not-yet-published-value skipping path.
"""

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import ons

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ons_cpi_core.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


_RealAsyncClient = httpx.AsyncClient


def _install_mock_client(monkeypatch, payload: dict, captured_urls: list, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(status_code, json=payload)

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(ons.httpx, "AsyncClient", factory)


def _base_config():
    return {
        "indicators": {
            "ons_base_url": "https://api.ons.gov.uk/v1/data",
            "fetch_timeout_seconds": 20,
        }
    }


def test_fetch_parses_monthly_observations_and_skips_unpublished_value(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_urls: list = []
    _install_mock_client(monkeypatch, _load_fixture(), captured_urls)

    series_spec = {
        "series_id": "gb.cpi_core",
        "provider_series_id": "economy/inflationandpriceindices/timeseries/dko8/mm23",
        "frequency": "M",
    }
    observations = asyncio.run(ons.fetch(series_spec))

    # Fixture has 3 months with one blank value -> 2 parsed.
    assert len(observations) == 2
    assert all(isinstance(o, Observation) for o in observations)

    first = observations[0]
    assert first.period_start.isoformat() == "2026-04-01"
    assert first.period_end.isoformat() == "2026-04-30"
    assert first.value == 2.5
    assert first.is_preliminary is False

    assert not any(o.period_start.isoformat() == "2026-05-01" for o in observations)

    assert len(captured_urls) == 1
    assert "uri=%2Feconomy%2Finflationandpriceindices%2Ftimeseries%2Fdko8%2Fmm23" in captured_urls[0]


def test_fetch_returns_empty_list_when_no_rows(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    _install_mock_client(monkeypatch, {"months": []}, [])

    series_spec = {
        "series_id": "gb.cpi_core",
        "provider_series_id": "economy/inflationandpriceindices/timeseries/dko8/mm23",
        "frequency": "M",
    }
    observations = asyncio.run(ons.fetch(series_spec))

    assert observations == []


def test_period_bounds_annual_and_quarterly():
    assert ons._period_bounds({"year": "2025"}, "A") == (date(2025, 1, 1), date(2025, 12, 31))
    start, end = ons._period_bounds({"year": "2025", "quarter": "Q2"}, "Q")
    assert start.isoformat() == "2025-04-01"
    assert end.isoformat() == "2025-06-30"
