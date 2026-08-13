"""Tests indicators/providers/oecd.py against a recorded fixture -- never a
live network call, mirrors tests/test_indicators_providers_eurostat.py's
pattern.

tests/fixtures/oecd_retail_sales_us.json is a trimmed copy of a real
response, fetched live 2026-08-13 from
https://sdmx.oecd.org/public/rest/data/all,DSD_STES@DF_INDSERV,/USA.M.TOVM.IX.G47.Y._Z._Z.N?format=jsondata.
"""

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import oecd

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "oecd_retail_sales_us.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


_RealAsyncClient = httpx.AsyncClient


def _install_mock_client(monkeypatch, payload: dict, captured_requests: list, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code, json=payload)

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(oecd.httpx, "AsyncClient", factory)


def _base_config():
    return {
        "indicators": {
            "oecd_base_url": "https://sdmx.oecd.org/public/rest/data",
            "fetch_timeout_seconds": 20,
        }
    }


def test_fetch_parses_monthly_observations(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_requests: list = []
    _install_mock_client(monkeypatch, _load_fixture(), captured_requests)

    series_spec = {
        "series_id": "us.retail_sales",
        "provider_series_id": "OECD/DSD_STES@DF_INDSERV/USA.M.TOVM.IX.G47.Y._Z._Z.N",
    }
    observations = asyncio.run(oecd.fetch(series_spec))

    assert len(observations) == 3
    assert all(isinstance(o, Observation) for o in observations)
    assert all(o.is_preliminary is False for o in observations)

    latest = next(o for o in observations if o.period_start == date(2026, 5, 1))
    assert latest.period_end.isoformat() == "2026-05-31"
    assert latest.value == 119.5644

    assert len(captured_requests) == 1
    request_url = str(captured_requests[0].url)
    assert "/all,DSD_STES@DF_INDSERV,/USA.M.TOVM.IX.G47.Y._Z._Z.N" in request_url
    assert "format=jsondata" in request_url


def test_fetch_returns_empty_list_when_no_series(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    empty_payload = {
        "data": {
            "structures": [{"dimensions": {"observation": [{"id": "TIME_PERIOD", "values": []}]}}],
            "dataSets": [{"series": {}}],
        }
    }
    _install_mock_client(monkeypatch, empty_payload, [])

    series_spec = {"series_id": "us.retail_sales", "provider_series_id": "OECD/DSD_STES@DF_INDSERV/BOGUS"}
    observations = asyncio.run(oecd.fetch(series_spec))

    assert observations == []


def test_fetch_skips_null_observations(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    payload = {
        "data": {
            "structures": [
                {"dimensions": {"observation": [{"id": "TIME_PERIOD", "values": [{"id": "2026-05"}, {"id": "2026-06"}]}]}}
            ],
            "dataSets": [{"series": {"0:0:0": {"observations": {"0": [100.0, 0], "1": [None, 0]}}}}],
        }
    }
    _install_mock_client(monkeypatch, payload, [])

    series_spec = {"series_id": "us.retail_sales", "provider_series_id": "OECD/DSD_STES@DF_INDSERV/USA.M.TOVM.IX.G47.Y._Z._Z.N"}
    observations = asyncio.run(oecd.fetch(series_spec))

    assert len(observations) == 1
    assert observations[0].period_start == date(2026, 5, 1)


def test_split_series_id_handles_at_sign_in_dataflow_id():
    dataflow_id, key = oecd._split_series_id("OECD/DSD_STES@DF_INDSERV/USA.M.TOVM.IX.G47.Y._Z._Z.N")
    assert dataflow_id == "DSD_STES@DF_INDSERV"
    assert key == "USA.M.TOVM.IX.G47.Y._Z._Z.N"


def test_parse_time_label_quarterly_and_annual():
    assert oecd._parse_time_label("2026-Q1") == (date(2026, 1, 1), date(2026, 3, 31))
    assert oecd._parse_time_label("2025") == (date(2025, 1, 1), date(2025, 12, 31))
