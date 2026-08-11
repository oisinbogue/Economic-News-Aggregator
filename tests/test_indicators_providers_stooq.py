"""Tests indicators/providers/stooq.py against recorded fixtures -- never a
live network call, mirrors tests/test_indicators_fx.py's Stooq CSV tests.

tests/fixtures/stooq_dax.csv is a hand-built CSV in Stooq's documented
`Date,Open,High,Low,Close,Volume` shape (the live endpoint itself could not
be captured for a real fixture -- see indicators/providers/stooq.py's
module docstring: every ticker this module backs returned Stooq's JS
bot-check challenge page, confirmed live 2026-08-10, not real CSV).
tests/fixtures/stooq_botcheck.html is a trimmed copy of that real
challenge-page response body.
"""

import asyncio
from datetime import date
from pathlib import Path

import httpx

import indicators.config as config_mod
from indicators.providers import Observation
from indicators.providers import stooq

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


_RealAsyncClient = httpx.AsyncClient


def _install_mock_client(monkeypatch, body: str, captured_urls: list, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(status_code, text=body)

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(stooq.httpx, "AsyncClient", factory)


def _base_config():
    return {"indicators": {"fetch_timeout_seconds": 20}}


def test_fetch_parses_daily_observations_and_skips_nd_row(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    captured_urls: list = []
    _install_mock_client(monkeypatch, _load_fixture("stooq_dax.csv"), captured_urls)

    series_spec = {"series_id": "de.equity_index", "provider_series_id": "^dax", "frequency": "D"}
    observations = asyncio.run(stooq.fetch(series_spec))

    # Fixture has 4 rows with one N/D close -> 3 parsed.
    assert len(observations) == 3
    assert all(isinstance(o, Observation) for o in observations)

    first = observations[0]
    assert first.period_start.isoformat() == "2026-08-06"
    assert first.period_end.isoformat() == "2026-08-06"
    assert first.value == 18550.0
    assert first.is_preliminary is False

    assert not any(o.period_start.isoformat() == "2026-08-08" for o in observations)

    assert len(captured_urls) == 1
    assert "s=%5Edax" in captured_urls[0]


def test_fetch_returns_empty_list_for_bot_check_page(monkeypatch):
    config_mod.get_config.cache_clear()
    monkeypatch.setattr(config_mod, "get_config", lambda: _base_config())

    _install_mock_client(monkeypatch, _load_fixture("stooq_botcheck.html"), [])

    series_spec = {"series_id": "de.equity_index", "provider_series_id": "^dax", "frequency": "D"}
    observations = asyncio.run(stooq.fetch(series_spec))

    assert observations == []


def test_parse_stooq_csv_skips_blank_and_nd_rows():
    csv_text = "Date,Open,High,Low,Close,Volume\n2026-08-06,1,2,1,100.5,1000\n2026-08-07,,,,N/D,\n"
    rows = stooq._parse_stooq_csv(csv_text)
    assert rows == [(date(2026, 8, 6), 100.5)]
