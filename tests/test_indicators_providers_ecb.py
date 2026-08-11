"""Tests indicators/providers/ecb.py against recorded fixtures -- never a
live network call, mirrors tests/test_indicators_providers.py's pattern.

tests/fixtures/ecb_policy_rate.json and ecb_hicp_monthly.json are trimmed
copies of real responses, fetched live 2026-08-10 from
https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV?format=jsondata
and .../data/IRS/M.U2.L.L40.CI.0000.EUR.N.Z?format=jsondata respectively.
"""

import asyncio
import json
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import ecb

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

    monkeypatch.setattr(ecb.httpx, "AsyncClient", factory)


def _base_config():
    return {
        "indicators": {
            "ecb_base_url": "https://data-api.ecb.europa.eu/service/data",
            "fetch_timeout_seconds": 20,
        }
    }


def test_fetch_parses_daily_observations_and_skips_null(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_urls: list = []
    _install_mock_client(monkeypatch, _load_fixture("ecb_policy_rate.json"), captured_urls)

    series_spec = {
        "series_id": "eu.policy_rate",
        "provider_series_id": "ECB/FM/D.U2.EUR.4F.KR.DFR.LEV",
        "frequency": "D",
    }
    observations = asyncio.run(ecb.fetch(series_spec))

    # Fixture has 6 observations with one null -> 5 parsed.
    assert len(observations) == 5
    assert all(isinstance(o, Observation) for o in observations)

    first = observations[0]
    assert first.period_start.isoformat() == "2026-08-05"
    assert first.period_end.isoformat() == "2026-08-05"
    assert first.value == 2.25
    assert first.is_preliminary is False

    assert not any(o.period_start.isoformat() == "2026-08-07" for o in observations)

    assert len(captured_urls) == 1
    assert captured_urls[0].startswith(
        "https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV?"
    )


def test_fetch_parses_monthly_observations_with_period_end_at_month_close(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    _install_mock_client(monkeypatch, _load_fixture("ecb_hicp_monthly.json"), [])

    series_spec = {
        "series_id": "eu.cpi_headline",
        "provider_series_id": "ECB/ICP/M.U2.N.000000.4.ANR",
        "frequency": "M",
    }
    observations = asyncio.run(ecb.fetch(series_spec))

    assert len(observations) == 3
    first = observations[0]
    assert first.period_start.isoformat() == "2026-04-01"
    assert first.period_end.isoformat() == "2026-04-30"
    assert first.value == 2.1


def test_fetch_returns_empty_list_when_no_series_in_response(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    empty_payload = {"dataSets": [{"series": {}}], "structure": {"dimensions": {"observation": [{"values": []}]}}}
    _install_mock_client(monkeypatch, empty_payload, [])

    series_spec = {
        "series_id": "eu.policy_rate",
        "provider_series_id": "ECB/FM/D.U2.EUR.4F.KR.DFR.LEV",
        "frequency": "D",
    }
    observations = asyncio.run(ecb.fetch(series_spec))

    assert observations == []


def test_split_series_id_rejects_malformed_id():
    import pytest

    with pytest.raises(ValueError):
        ecb._split_series_id("ECB/FM")


def test_parse_time_period_handles_all_granularities():
    assert ecb._parse_time_period("2026-08-07").isoformat() == "2026-08-07"
    assert ecb._parse_time_period("2026-08").isoformat() == "2026-08-01"
    assert ecb._parse_time_period("2026-Q3").isoformat() == "2026-07-01"
    assert ecb._parse_time_period("2026").isoformat() == "2026-01-01"
