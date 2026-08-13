"""Tests indicators/providers/bls.py against a recorded fixture -- never a
live network call, mirrors tests/test_indicators_providers_ons.py's pattern.

tests/fixtures/bls_cpi_headline.json is a trimmed copy of a real response,
fetched live 2026-08-13 from
https://api.bls.gov/publicAPI/v2/timeseries/data/CUSR0000SA0, with an
"M13" (annual-average) row and a "-" (footnoted/unavailable) row added to
exercise the skip paths, and a "P" (preliminary) footnote added to the
latest month.
"""

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import bls

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "bls_cpi_headline.json"


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

    monkeypatch.setattr(bls.httpx, "AsyncClient", factory)


def _base_config():
    return {"indicators": {"fetch_timeout_seconds": 20}}


def test_fetch_parses_monthly_observations_and_skips_unavailable_and_annual_rows(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    monkeypatch.setattr(bls, "_load_dotenv", lambda: None)

    captured_requests: list = []
    _install_mock_client(monkeypatch, _load_fixture(), captured_requests)

    series_spec = {"series_id": "us.cpi_headline", "provider_series_id": "CUSR0000SA0"}
    observations = asyncio.run(bls.fetch(series_spec))

    # Fixture has 5 rows: M13 (annual, skipped) and one "-" value (skipped) -> 3 parsed.
    assert len(observations) == 3
    assert all(isinstance(o, Observation) for o in observations)

    latest = next(o for o in observations if o.period_start == date(2026, 7, 1))
    assert latest.period_end.isoformat() == "2026-07-31"
    assert latest.value == 332.813
    assert latest.is_preliminary is True

    june = next(o for o in observations if o.period_start == date(2026, 6, 1))
    assert june.is_preliminary is False

    assert not any(o.period_start == date(2025, 10, 1) for o in observations)
    assert not any(o.period_start.month == 1 and o.period_start.year == 2025 and o.value == 320.5 for o in observations)

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.url.path == "/publicAPI/v2/timeseries/data/CUSR0000SA0"
    assert "registrationkey" not in str(request.url)


def test_fetch_returns_empty_list_when_no_series(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    monkeypatch.setattr(bls, "_load_dotenv", lambda: None)

    _install_mock_client(monkeypatch, {"status": "REQUEST_SUCCEEDED", "Results": {"series": []}}, [])

    series_spec = {"series_id": "us.cpi_headline", "provider_series_id": "CUSR0000SA0"}
    observations = asyncio.run(bls.fetch(series_spec))

    assert observations == []


def test_fetch_raises_on_request_not_succeeded(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    monkeypatch.setattr(bls, "_load_dotenv", lambda: None)

    _install_mock_client(
        monkeypatch,
        {"status": "REQUEST_NOT_PROCESSED", "message": ["Series does not exist"]},
        [],
    )

    series_spec = {"series_id": "us.cpi_headline", "provider_series_id": "BOGUS"}
    try:
        asyncio.run(bls.fetch(series_spec))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Series does not exist" in str(exc)


def test_fetch_uses_registered_key_and_year_range_when_env_var_set(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())
    monkeypatch.setenv("BLS_API_KEY", "test-key-123")
    monkeypatch.setattr(bls, "_load_dotenv", lambda: None)

    captured_requests: list = []
    _install_mock_client(monkeypatch, _load_fixture(), captured_requests)

    series_spec = {"series_id": "us.cpi_headline", "provider_series_id": "CUSR0000SA0"}
    asyncio.run(bls.fetch(series_spec))

    assert len(captured_requests) == 1
    query = str(captured_requests[0].url)
    assert "registrationkey=test-key-123" in query
    assert "startyear=" in query and "endyear=" in query
