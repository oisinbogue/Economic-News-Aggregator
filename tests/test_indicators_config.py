import yaml

import indicators.config as config_mod


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _minimal_config(tmp_path):
    """Builds a minimal-but-valid indicators config tree (2 countries, 2 core
    indicators) under tmp_path, mirroring the real config/indicators/ shape.
    """
    indicators_dir = tmp_path / "indicators"
    series_dir = indicators_dir / "series"
    series_dir.mkdir(parents=True)

    _write_yaml(indicators_dir / "countries.yaml", {
        "countries": {
            "us": {"iso": "us", "name": "United States", "currency": "USD", "timezone": "America/New_York"},
            "ie": {"iso": "ie", "name": "Ireland", "currency": "EUR", "timezone": "Europe/Dublin"},
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

    _write_yaml(series_dir / "us.yaml", {
        "country": "us",
        "series": {
            "gdp_real": {
                "provider": "dbnomics", "provider_series_id": "FRED/GDPC1",
                "fallback_providers": [], "source_agency": "BEA",
                "source_url": "https://www.bea.gov/data/gdp", "licence": "public-domain",
                "seasonal_adjustment": "sa", "revisable": True,
                "news_topic_tags": ["MACROECONOMICS"],
            },
            "cpi_headline": {
                "provider": "dbnomics", "provider_series_id": "FRED/CPIAUCSL",
                "fallback_providers": [], "source_agency": "BLS",
                "source_url": "https://www.bls.gov/cpi/", "licence": "public-domain",
                "seasonal_adjustment": "sa", "revisable": True,
                "news_topic_tags": ["MACROECONOMICS"],
            },
        },
        "extras": [],
    })

    _write_yaml(series_dir / "ie.yaml", {
        "country": "ie",
        "series": {
            "gdp_real": {
                "provider": "dbnomics", "provider_series_id": "FRED/NAEXKP01IEQ657S",
                "fallback_providers": [], "source_agency": "CSO",
                "source_url": "https://www.cso.ie/", "licence": "cc-by-4.0",
                "seasonal_adjustment": "sa", "revisable": True,
                "news_topic_tags": ["IRELAND", "MACROECONOMICS"],
            },
            "cpi_headline": {
                "unavailable": True,
                "unavailable_reason": "test fixture placeholder",
                "news_topic_tags": ["IRELAND"],
            },
        },
        "extras": [],
    })

    return indicators_dir


def _patch_dirs(monkeypatch, tmp_path, indicators_dir, taxonomy_themes=("IRELAND", "MACROECONOMICS")):
    taxonomy_path = tmp_path / "taxonomy.yaml"
    _write_yaml(taxonomy_path, {"keywords": {theme: {"active": [], "disabled": []} for theme in taxonomy_themes}})

    monkeypatch.setattr(config_mod, "INDICATORS_CONFIG_DIR", indicators_dir)
    monkeypatch.setattr(config_mod, "TAXONOMY_PATH", taxonomy_path)
    config_mod.get_countries.cache_clear()
    config_mod.get_core_indicators.cache_clear()
    config_mod.get_series.cache_clear()
    config_mod.get_taxonomy_themes.cache_clear()


def test_valid_config_passes(tmp_path, monkeypatch):
    indicators_dir = _minimal_config(tmp_path)
    _patch_dirs(monkeypatch, tmp_path, indicators_dir)

    assert config_mod.validate() == []


def test_missing_core_indicator_for_country_fails(tmp_path, monkeypatch):
    indicators_dir = _minimal_config(tmp_path)
    # Drop cpi_headline entirely from ie.yaml -- not even marked unavailable.
    ie_path = indicators_dir / "series" / "ie.yaml"
    data = yaml.safe_load(ie_path.read_text(encoding="utf-8"))
    del data["series"]["cpi_headline"]
    _write_yaml(ie_path, data)

    _patch_dirs(monkeypatch, tmp_path, indicators_dir)

    problems = config_mod.validate()
    assert any("ie.cpi_headline" in p and "missing from series" in p for p in problems)


def test_unknown_news_topic_tag_fails(tmp_path, monkeypatch):
    indicators_dir = _minimal_config(tmp_path)
    us_path = indicators_dir / "series" / "us.yaml"
    data = yaml.safe_load(us_path.read_text(encoding="utf-8"))
    data["series"]["gdp_real"]["news_topic_tags"] = ["NOT_A_REAL_THEME"]
    _write_yaml(us_path, data)

    _patch_dirs(monkeypatch, tmp_path, indicators_dir)

    problems = config_mod.validate()
    assert any("us.gdp_real" in p and "news_topic_tags" in p for p in problems)


def test_series_missing_required_field_fails(tmp_path, monkeypatch):
    indicators_dir = _minimal_config(tmp_path)
    core_path = indicators_dir / "core.yaml"
    data = yaml.safe_load(core_path.read_text(encoding="utf-8"))
    del data["indicators"]["gdp_real"]["direction_good"]
    _write_yaml(core_path, data)

    _patch_dirs(monkeypatch, tmp_path, indicators_dir)

    problems = config_mod.validate()
    assert any("us.gdp_real" in p and "direction_good" in p for p in problems)
    assert any("ie.gdp_real" in p and "direction_good" in p for p in problems)
