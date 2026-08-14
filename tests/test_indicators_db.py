import sqlite3

import pytest
import yaml

import indicators.config as config_mod
import indicators.db as db_mod


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_config_tree(tmp_path):
    """Builds a minimal indicators config tree under tmp_path: 5 Phase-1
    countries x 2 core indicators (one with a real provider mapping, one
    unavailable in a couple of countries), mirroring config/indicators/'s
    real shape closely enough to exercise sync_series_from_config().
    """
    indicators_dir = tmp_path / "indicators"
    series_dir = indicators_dir / "series"
    series_dir.mkdir(parents=True)

    isos = ["us", "gb", "ie", "eu", "de"]
    _write_yaml(indicators_dir / "countries.yaml", {
        "countries": {
            iso: {"iso": iso, "name": iso.upper(), "currency": "USD", "timezone": "UTC"}
            for iso in isos
        }
    })

    _write_yaml(indicators_dir / "core.yaml", {
        "indicators": {
            "gdp_real": {
                "topic": "economy", "unit": "percent", "frequency": "Q",
                "transform_default": "yoy", "direction_good": "up",
            },
            "cpi_headline": {
                "topic": "prices", "unit": "percent", "frequency": "M",
                "transform_default": "yoy", "direction_good": "neutral",
                "target_band": [1.0, 3.0],
            },
        }
    })

    for iso in isos:
        _write_yaml(series_dir / f"{iso}.yaml", {
            "country": iso,
            "series": {
                "gdp_real": {
                    "provider": "dbnomics", "provider_series_id": f"FRED/{iso.upper()}GDP",
                    "fallback_providers": [], "source_agency": "Some Agency",
                    "source_url": "https://example.com/gdp", "licence": "public-domain",
                    "seasonal_adjustment": "sa", "revisable": True,
                    "news_topic_tags": [],
                },
                "cpi_headline": {
                    "unavailable": True,
                    "unavailable_reason": "test fixture placeholder",
                    "news_topic_tags": [],
                },
            },
            "extras": [],
        })

    return indicators_dir


@pytest.fixture
def indicators_env(tmp_path, monkeypatch):
    """Points both indicators.config and indicators.db at an isolated
    tmp_path config tree + db file, and clears the config lru_caches so
    each test starts clean.
    """
    indicators_dir = _write_config_tree(tmp_path)
    root_config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "data" / "indicators.db"
    _write_yaml(root_config_path, {"paths": {"indicators_db_file": str(db_path)}})

    monkeypatch.setattr(config_mod, "INDICATORS_CONFIG_DIR", indicators_dir)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", root_config_path)
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    config_mod.get_config.cache_clear()
    config_mod.get_countries.cache_clear()
    config_mod.get_core_indicators.cache_clear()
    config_mod.get_series.cache_clear()

    return {"db_path": db_path, "isos": ["us", "gb", "ie", "eu", "de"]}


def test_init_db_creates_schema_cleanly(indicators_env):
    db_mod.init_db()
    with db_mod.get_connection() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables >= {
        "series", "observations", "observation_revisions",
        "releases", "fetch_log", "fx_rates",
    }


def test_sync_populates_expected_row_count(indicators_env):
    db_mod.init_db()
    n = db_mod.sync_series_from_config()
    # 5 countries x 2 core indicators (1 real + 1 unavailable) = 10 rows.
    assert n == 10
    with db_mod.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"]
    assert count == 10


def test_sync_marks_unavailable_series_inactive_with_reason(indicators_env):
    db_mod.init_db()
    db_mod.sync_series_from_config()
    with db_mod.get_connection() as conn:
        row = conn.execute(
            "SELECT active, unavailable_reason FROM series WHERE series_id = 'us.cpi_headline'"
        ).fetchone()
    assert row["active"] == 0
    assert row["unavailable_reason"] == "test fixture placeholder"


def test_sync_populates_available_series_fields(indicators_env):
    db_mod.init_db()
    db_mod.sync_series_from_config()
    with db_mod.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM series WHERE series_id = 'us.gdp_real'"
        ).fetchone()
    assert row["active"] == 1
    assert row["provider"] == "dbnomics"
    assert row["provider_series_id"] == "FRED/USGDP"
    assert row["tier"] == "core"
    assert row["topic"] == "economy"


def test_check_constraint_rejects_invalid_topic(indicators_env):
    db_mod.init_db()
    with db_mod.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO series (
                    series_id, country, indicator_key, title, topic, tier, unit,
                    frequency, transform_default, direction_good, provider,
                    provider_series_id, source_agency, source_url, licence
                ) VALUES (
                    'xx.bad_topic', 'xx', 'bad_topic', 'Bad Topic', 'not_a_real_topic', 'core',
                    'percent', 'M', 'yoy', 'up', 'dbnomics', 'X/Y', 'agency', 'https://x', 'licence'
                )
                """
            )


def test_check_constraint_rejects_invalid_unit(indicators_env):
    db_mod.init_db()
    with db_mod.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO series (
                    series_id, country, indicator_key, title, topic, tier, unit,
                    frequency, transform_default, direction_good, provider,
                    provider_series_id, source_agency, source_url, licence
                ) VALUES (
                    'xx.bad_unit', 'xx', 'bad_unit', 'Bad Unit', 'economy', 'core',
                    'not_a_real_unit', 'M', 'yoy', 'up', 'dbnomics', 'X/Y', 'agency', 'https://x', 'licence'
                )
                """
            )


def test_sync_upsert_does_not_duplicate_on_second_run(indicators_env):
    db_mod.init_db()
    db_mod.sync_series_from_config()
    db_mod.sync_series_from_config()
    with db_mod.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"]
    assert count == 10


def test_sync_upsert_updates_changed_fields_in_place(indicators_env, tmp_path):
    db_mod.init_db()
    db_mod.sync_series_from_config()

    us_path = tmp_path / "indicators" / "series" / "us.yaml"
    data = yaml.safe_load(us_path.read_text(encoding="utf-8"))
    data["series"]["gdp_real"]["provider_series_id"] = "FRED/CHANGED"
    _write_yaml(us_path, data)
    config_mod.get_series.cache_clear()

    db_mod.sync_series_from_config()

    with db_mod.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"]
        row = conn.execute(
            "SELECT provider_series_id FROM series WHERE series_id = 'us.gdp_real'"
        ).fetchone()
    assert count == 10
    assert row["provider_series_id"] == "FRED/CHANGED"


def _log_ok_fetch(conn, series_id: str) -> None:
    conn.execute(
        "INSERT INTO fetch_log (series_id, run_at, status, detail) VALUES (?, ?, 'ok', NULL)",
        (series_id, "2026-08-11T13:23:00+00:00"),
    )


def test_sync_clears_ok_fetch_log_for_a_series_that_changed_source(indicators_env, tmp_path):
    """Regression: the 2026-08-13 provider migration shipped correct config
    and provider modules that never ran, because `indicators.fetch`'s
    incremental due-check saw a recent status='ok' row from the OLD
    provider and skipped the series.
    """
    db_mod.init_db()
    db_mod.sync_series_from_config()
    with db_mod.get_connection() as conn:
        _log_ok_fetch(conn, "us.gdp_real")
        _log_ok_fetch(conn, "us.cpi_headline")

    us_path = tmp_path / "indicators" / "series" / "us.yaml"
    data = yaml.safe_load(us_path.read_text(encoding="utf-8"))
    data["series"]["gdp_real"]["provider"] = "oecd"
    _write_yaml(us_path, data)
    config_mod.get_series.cache_clear()

    db_mod.sync_series_from_config()

    with db_mod.get_connection() as conn:
        moved = conn.execute(
            "SELECT COUNT(*) AS n FROM fetch_log WHERE series_id = 'us.gdp_real' AND status = 'ok'"
        ).fetchone()["n"]
        untouched = conn.execute(
            "SELECT COUNT(*) AS n FROM fetch_log WHERE series_id = 'us.cpi_headline' AND status = 'ok'"
        ).fetchone()["n"]

    # The migrated series looks never-successfully-fetched, so it is due
    # immediately; its unchanged neighbour keeps its history.
    assert moved == 0
    assert untouched == 1
