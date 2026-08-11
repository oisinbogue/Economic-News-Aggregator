"""Loads + validates config/indicators/*.yaml once per process.

Mirrors pipeline/config.py's get_config()/resolve_path() pattern (lru_cache,
PyYAML safe_load, no reload hatch -- see INDICATORS_SCHEMA_PROPOSAL.md open
question 3), but is deliberately self-contained rather than importing
pipeline/config.py: the indicators subsystem is a separate failure domain
from the news pipeline (INDICATORS_BRIEF.md §2), so it should not depend on
pipeline/ modules at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
INDICATORS_CONFIG_DIR = REPO_ROOT / "config" / "indicators"
TAXONOMY_PATH = REPO_ROOT / "config" / "taxonomy.yaml"

REQUIRED_SERIES_FIELDS = ("unit", "frequency", "transform_default", "direction_good")


@lru_cache(maxsize=1)
def get_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(relative: str) -> Path:
    """Resolves a path from config.yaml (given relative to the repo root)."""
    return (REPO_ROOT / relative).resolve()


@lru_cache(maxsize=1)
def get_countries() -> dict:
    """Returns config/indicators/countries.yaml's `countries:` mapping, keyed by iso."""
    path = INDICATORS_CONFIG_DIR / "countries.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("countries", {})


@lru_cache(maxsize=1)
def get_core_indicators() -> dict:
    """Returns config/indicators/core.yaml's `indicators:` mapping, keyed by indicator_key."""
    path = INDICATORS_CONFIG_DIR / "core.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("indicators", {})


@lru_cache(maxsize=None)
def get_series(country_iso: str) -> dict:
    """Returns config/indicators/series/<iso>.yaml's `series:` mapping, keyed by indicator_key."""
    path = INDICATORS_CONFIG_DIR / "series" / f"{country_iso}.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("series", {})


@lru_cache(maxsize=1)
def get_taxonomy_themes() -> set[str]:
    """Returns the closed set of theme names from config/taxonomy.yaml's `keywords:` top-level keys."""
    with open(TAXONOMY_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return set(data.get("keywords", {}).keys())


def validate() -> list[str]:
    """Validates the indicators config tree. Returns a list of problem strings
    (empty = valid). Called by both `python -m indicators.config` (manual
    check) and as the first step of fetch.py/build.py, which exit non-zero on
    any problem rather than proceeding with partial config -- matches the
    brief's "only a config-validation error should hard-fail" rule.
    """
    problems: list[str] = []

    countries = get_countries()
    core_indicators = get_core_indicators()
    taxonomy_themes = get_taxonomy_themes()

    for iso in countries:
        try:
            series = get_series(iso)
        except FileNotFoundError:
            problems.append(f"{iso}: missing config/indicators/series/{iso}.yaml")
            continue

        for indicator_key, core_def in core_indicators.items():
            entry = series.get(indicator_key)
            if entry is None:
                problems.append(
                    f"{iso}.{indicator_key}: core indicator missing from series/{iso}.yaml "
                    "(must be present, either with a provider mapping or unavailable: true)"
                )
                continue

            if entry.get("unavailable"):
                if not entry.get("unavailable_reason"):
                    problems.append(f"{iso}.{indicator_key}: unavailable but missing unavailable_reason")
                continue

            # unit/frequency/transform_default/direction_good are defined
            # country-agnostically on core.yaml and may be overridden per
            # country in series/<iso>.yaml (e.g. target_band); validate the
            # resolved definition, not the series entry in isolation.
            resolved = {**core_def, **entry}
            for field in REQUIRED_SERIES_FIELDS:
                if resolved.get(field) is None:
                    problems.append(f"{iso}.{indicator_key}: missing required field '{field}'")

            tags = entry.get("news_topic_tags", [])
            unknown_tags = [t for t in tags if t not in taxonomy_themes]
            if unknown_tags:
                problems.append(
                    f"{iso}.{indicator_key}: news_topic_tags contains values outside the "
                    f"taxonomy's 8 themes: {unknown_tags}"
                )

    return problems


if __name__ == "__main__":
    problems = validate()
    if not problems:
        print("indicators config: OK")
    else:
        print(f"indicators config: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
