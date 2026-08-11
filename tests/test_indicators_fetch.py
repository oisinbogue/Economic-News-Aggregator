"""Tests indicators/fetch.py's incremental vs --backfill series selection
(INDICATORS_BRIEF.md §8/§13 deliverable 4). Pure unit tests against
`_is_due_for_refresh()`/`_select_due_series()` plus an end-to-end
`fetch_all()` check with a mocked DBnomics provider -- never live network,
same convention as tests/test_indicators_providers.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

import indicators.config as config_mod
import indicators.db as db_mod
import indicators.fetch as fetch_mod
from indicators.providers import dbnomics


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_config_tree(tmp_path):
    indicators_dir = tmp_path / "indicators"
    series_dir = indicators_dir / "series"
    series_dir.mkdir(parents=True)

    _write_yaml(indicators_dir / "countries.yaml", {
        "countries": {"us": {"iso": "us", "name": "United States", "currency": "USD", "timezone": "UTC"}}
    })
    _write_yaml(indicators_dir / "core.yaml", {
        "indicators": {
            "gdp_real": {
                "topic": "economy", "unit": "percent", "frequency": "Q",
                "transform_default": "yoy", "direction_good": "up",
            },
            "policy_rate": {
                "topic": "rates", "unit": "percent", "frequency": "D",
                "transform_default": "level", "direction_good": "neutral",
            },
        }
    })
    _write_yaml(series_dir / "us.yaml", {
        "country": "us",
        "series": {
            "gdp_real": {
                "provider": "dbnomics", "provider_series_id": "BEA/NIPA-T10105/A006RC-Q",
                "fallback_providers": [], "source_agency": "BEA",
                "source_url": "https://example.com", "licence": "public-domain",
                "seasonal_adjustment": "sa", "revisable": True, "news_topic_tags": [],
            },
            "policy_rate": {
                "provider": "dbnomics", "provider_series_id": "FED/H15",
                "fallback_providers": [], "source_agency": "Fed",
                "source_url": "https://example.com", "licence": "public-domain",
                "seasonal_adjustment": "nsa", "revisable": False, "news_topic_tags": [],
            },
        },
        "extras": [],
    })
    return indicators_dir


@pytest.fixture
def indicators_env(tmp_path, monkeypatch):
    indicators_dir = _write_config_tree(tmp_path)
    root_config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "data" / "indicators.db"
    _write_yaml(root_config_path, {
        "paths": {"indicators_db_file": str(db_path)},
        "indicators": {
            "dbnomics_base_url": "https://api.db.nomics.world/v22/",
            "fetch_concurrency": 5,
            "fetch_timeout_seconds": 20,
        },
    })

    monkeypatch.setattr(config_mod, "INDICATORS_CONFIG_DIR", indicators_dir)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", root_config_path)
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    config_mod.get_config.cache_clear()
    config_mod.get_countries.cache_clear()
    config_mod.get_core_indicators.cache_clear()
    config_mod.get_series.cache_clear()

    db_mod.init_db()
    db_mod.sync_series_from_config()

    return {"db_path": db_path}


# --------------------------------------------------------------------------
# _is_due_for_refresh
# --------------------------------------------------------------------------

def test_series_with_no_prior_fetch_is_always_due():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    assert fetch_mod._is_due_for_refresh("A", None, now) is True


def test_daily_series_not_due_within_interval():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(hours=5)
    assert fetch_mod._is_due_for_refresh("D", last_ok, now) is False


def test_daily_series_due_after_interval():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(hours=21)
    assert fetch_mod._is_due_for_refresh("D", last_ok, now) is True


def test_quarterly_series_not_due_after_one_day():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=1)
    assert fetch_mod._is_due_for_refresh("Q", last_ok, now) is False


def test_quarterly_series_due_after_three_weeks():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=22)
    assert fetch_mod._is_due_for_refresh("Q", last_ok, now) is True


# --------------------------------------------------------------------------
# _select_due_series / fetch_all(backfill=...)
# --------------------------------------------------------------------------

def test_select_due_series_skips_recently_ok_series(indicators_env):
    now = datetime.now(timezone.utc)
    with db_mod.get_connection() as conn:
        conn.execute(
            "INSERT INTO fetch_log (series_id, run_at, status, detail) VALUES (?, ?, 'ok', NULL)",
            ("us.policy_rate", now.isoformat()),
        )
        series_rows = [dict(row) for row in conn.execute("SELECT * FROM series WHERE active = 1")]
        due = fetch_mod._select_due_series(conn, series_rows, now)

    due_ids = {row["series_id"] for row in due}
    assert "us.policy_rate" not in due_ids  # daily, fetched moments ago
    assert "us.gdp_real" in due_ids  # never fetched -> always due


def _install_mock_dbnomics(monkeypatch, captured_urls):
    payload = {
        "errors": None,
        "series": {"docs": [{"period_start_day": ["2024-01-01"], "value": [100.0]}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(dbnomics.httpx, "AsyncClient", factory)


async def _no_ecb_rates():
    return []


async def _no_stooq_rate(_currency):
    return None


def test_backfill_refetches_series_fetched_moments_ago(indicators_env, monkeypatch):
    captured_urls: list[str] = []
    _install_mock_dbnomics(monkeypatch, captured_urls)
    monkeypatch.setattr(fetch_mod.fx, "fetch_ecb_rates", _no_ecb_rates)
    monkeypatch.setattr(fetch_mod.fx, "fetch_stooq_rate", _no_stooq_rate)

    now = datetime.now(timezone.utc)
    with db_mod.get_connection() as conn:
        conn.execute(
            "INSERT INTO fetch_log (series_id, run_at, status, detail) VALUES (?, ?, 'ok', NULL)",
            ("us.policy_rate", now.isoformat()),
        )
        conn.execute(
            "INSERT INTO fetch_log (series_id, run_at, status, detail) VALUES (?, ?, 'ok', NULL)",
            ("us.gdp_real", now.isoformat()),
        )

    incremental_results = asyncio.run(fetch_mod.fetch_all(concurrency=5, backfill=False))
    assert incremental_results == []  # both series were just fetched -> nothing due

    backfill_results = asyncio.run(fetch_mod.fetch_all(concurrency=5, backfill=True))
    fetched_ids = {series_id for series_id, _, _, _ in backfill_results}
    assert fetched_ids == {"us.gdp_real", "us.policy_rate"}
