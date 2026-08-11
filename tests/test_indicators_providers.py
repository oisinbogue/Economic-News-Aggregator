"""Tests indicators/providers/dbnomics.py against a recorded fixture --
never a live network call, per INDICATORS_SCHEMA_PROPOSAL.md's VCR-style
testing convention (mirrors tests/test_fetch.py's asyncio.run()-in-a-sync-test
pattern since pytest-asyncio isn't a project dependency).

tests/fixtures/dbnomics_gdpc1.json is a trimmed copy of a real response,
fetched live 2026-08-10 from
https://api.db.nomics.world/v22/series/BEA/NIPA-T10105?observations=1&limit=100
(quarterly "Gross private domestic investment" series A006RC-Q) -- 8
quarters kept, with one value set to null to exercise the null-skipping
path.
"""

import asyncio
import json
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import dbnomics

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dbnomics_gdpc1.json"


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

    monkeypatch.setattr(dbnomics.httpx, "AsyncClient", factory)


def _base_config():
    return {
        "indicators": {
            "dbnomics_base_url": "https://api.db.nomics.world/v22/",
            "fetch_timeout_seconds": 20,
        }
    }


def test_fetch_parses_observations_from_fixture(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_urls: list = []
    _install_mock_client(monkeypatch, _load_fixture(), captured_urls)

    series_spec = {
        "series_id": "us.gdp_real",
        "provider_series_id": "BEA/NIPA-T10105/A006RC-Q",
        "frequency": "Q",
    }
    observations = asyncio.run(dbnomics.fetch(series_spec))

    # Fixture has 8 quarterly observations with one null value -> 7 parsed.
    assert len(observations) == 7
    assert all(isinstance(o, Observation) for o in observations)

    first = observations[0]
    assert first.period_start.isoformat() == "2024-07-01"
    assert first.period_end.isoformat() == "2024-09-30"
    assert first.value == 5330249.0
    assert first.is_preliminary is False

    # The null-valued period (2025-Q1, period_start 2025-01-01) is skipped.
    assert not any(o.period_start.isoformat() == "2025-01-01" for o in observations)

    assert len(captured_urls) == 1
    assert captured_urls[0].startswith(
        "https://api.db.nomics.world/v22/series/BEA/NIPA-T10105/A006RC-Q"
    )


def test_fetch_annual_frequency_computes_period_end_one_year_after_start(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    _install_mock_client(monkeypatch, _load_fixture(), [])

    series_spec = {
        "series_id": "us.gdp_real",
        "provider_series_id": "BEA/NIPA-T10105/A006RC-Q",
        "frequency": "A",
    }
    observations = asyncio.run(dbnomics.fetch(series_spec))

    first = observations[0]
    assert first.period_start.isoformat() == "2024-07-01"
    assert first.period_end.isoformat() == "2024-12-31"


def test_fetch_uses_dataset_listing_endpoint_for_two_segment_id(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_urls: list = []
    _install_mock_client(monkeypatch, _load_fixture(), captured_urls)

    series_spec = {
        "series_id": "us.gdp_real",
        "provider_series_id": "FED/H15",
        "frequency": "D",
    }
    asyncio.run(dbnomics.fetch(series_spec))

    assert len(captured_urls) == 1
    assert captured_urls[0].startswith("https://api.db.nomics.world/v22/series/FED/H15?")


def test_fetch_returns_empty_list_when_no_docs(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    empty_payload = {"errors": None, "series": {"docs": [], "limit": 1, "num_found": 0, "offset": 0}}
    _install_mock_client(monkeypatch, empty_payload, [])

    series_spec = {
        "series_id": "us.gdp_real",
        "provider_series_id": "BEA/NIPA-T10105/A006RC-Q",
        "frequency": "Q",
    }
    observations = asyncio.run(dbnomics.fetch(series_spec))

    assert observations == []


def test_fetch_skips_na_string_values(monkeypatch):
    """Some upstream providers mirrored via DBnomics (e.g. IMF WEO) represent
    a missing observation as the string "NA" instead of JSON null -- this
    must be skipped like a null, not raise ValueError from float("NA").
    """
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    payload = {
        "errors": None,
        "series": {
            "docs": [
                {
                    "period_start_day": ["2023-01-01", "2024-01-01", "2025-01-01"],
                    "value": ["NA", 34.346, "NA"],
                }
            ]
        },
    }
    _install_mock_client(monkeypatch, payload, [])

    series_spec = {
        "series_id": "ie.govt_debt_gdp",
        "provider_series_id": "IMF/WEO:latest/IRL.GGXWDG_NGDP",
        "frequency": "A",
    }
    observations = asyncio.run(dbnomics.fetch(series_spec))

    assert len(observations) == 1
    assert observations[0].period_start.isoformat() == "2024-01-01"
    assert observations[0].value == 34.346


def test_fetch_raises_on_provider_error(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    error_payload = {"message": "Could not find storage directory for provider 'FRED'"}
    _install_mock_client(monkeypatch, error_payload, [])

    series_spec = {
        "series_id": "us.gdp_real",
        "provider_series_id": "FRED/GDPC1",
        "frequency": "Q",
    }
    try:
        asyncio.run(dbnomics.fetch(series_spec))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "FRED/GDPC1" in str(exc)
