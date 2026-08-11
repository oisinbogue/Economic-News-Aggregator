"""Tests indicators/providers/eurostat.py against recorded fixtures -- never
a live network call, mirrors tests/test_indicators_providers.py's pattern.

tests/fixtures/eurostat_hicp_core.json and eurostat_house_prices.json are
trimmed copies of real JSON-stat responses, fetched live 2026-08-10 from
https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/prc_hicp_manr/M.RCH_A.TOT_X_NRG_FOOD_NP.EA20?format=JSON
and .../data/prc_hpi_q/Q.TOTAL.I15_Q.EA20?format=JSON respectively.
"""

import asyncio
import json
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import eurostat

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


_RealAsyncClient = httpx.AsyncClient


def _install_mock_client(monkeypatch, payload: dict, captured_urls: list, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(status_code, json=payload)

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(eurostat.httpx, "AsyncClient", factory)


def _base_config():
    return {
        "indicators": {
            "eurostat_base_url": "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data",
            "fetch_timeout_seconds": 20,
        }
    }


def test_fetch_parses_monthly_observations_and_skips_null(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_urls: list = []
    _install_mock_client(monkeypatch, _load_fixture("eurostat_hicp_core.json"), captured_urls)

    series_spec = {
        "series_id": "eu.cpi_core",
        "provider_series_id": "Eurostat/prc_hicp_manr/M.RCH_A.TOT_X_NRG_FOOD_NP.EA20",
        "frequency": "M",
    }
    observations = asyncio.run(eurostat.fetch(series_spec))

    # Fixture has 3 observations with one null -> 2 parsed.
    assert len(observations) == 2
    assert all(isinstance(o, Observation) for o in observations)

    first = observations[0]
    assert first.period_start.isoformat() == "2026-04-01"
    assert first.period_end.isoformat() == "2026-04-30"
    assert first.value == 2.3
    assert first.is_preliminary is False

    assert not any(o.period_start.isoformat() == "2026-05-01" for o in observations)

    assert len(captured_urls) == 1
    assert captured_urls[0].startswith(
        "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/prc_hicp_manr/"
        "M.RCH_A.TOT_X_NRG_FOOD_NP.EA20?"
    )


def test_fetch_parses_quarterly_observations(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    _install_mock_client(monkeypatch, _load_fixture("eurostat_house_prices.json"), [])

    series_spec = {
        "series_id": "eu.house_prices",
        "provider_series_id": "Eurostat/prc_hpi_q/Q.TOTAL.I15_Q.EA20",
        "frequency": "Q",
    }
    observations = asyncio.run(eurostat.fetch(series_spec))

    assert len(observations) == 3
    first = observations[0]
    assert first.period_start.isoformat() == "2025-07-01"
    assert first.period_end.isoformat() == "2025-09-30"
    assert first.value == 143.2


def test_fetch_returns_empty_list_when_no_values(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    empty_payload = {"value": {}, "dimension": {"time": {"category": {"index": {}}}}}
    _install_mock_client(monkeypatch, empty_payload, [])

    series_spec = {
        "series_id": "eu.cpi_core",
        "provider_series_id": "Eurostat/prc_hicp_manr/M.RCH_A.TOT_X_NRG_FOOD_NP.EA20",
        "frequency": "M",
    }
    observations = asyncio.run(eurostat.fetch(series_spec))

    assert observations == []


def test_fetch_raises_on_provider_error(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    error_payload = {"error": [{"status": 400, "label": "INVALID_QUERY_NB_FILTERS"}]}
    _install_mock_client(monkeypatch, error_payload, [])

    series_spec = {
        "series_id": "eu.cpi_core",
        "provider_series_id": "Eurostat/prc_hicp_manr/BAD.KEY",
        "frequency": "M",
    }
    try:
        asyncio.run(eurostat.fetch(series_spec))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "BAD.KEY" in str(exc)


def test_parse_time_label_handles_all_granularities():
    assert eurostat._parse_time_label("2026-08").isoformat() == "2026-08-01"
    assert eurostat._parse_time_label("2026-Q3").isoformat() == "2026-07-01"
    assert eurostat._parse_time_label("2026").isoformat() == "2026-01-01"
