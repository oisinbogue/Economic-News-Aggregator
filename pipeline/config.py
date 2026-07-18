"""Loads config.yaml once per process and resolves its relative paths.

All pipeline paths in config.yaml are written relative to the repo root so
the config is portable between a laptop checkout and the GitHub Actions
runner. `resolve_path` is how every other module turns those into absolute
paths without duplicating the "relative to what?" logic.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


@lru_cache(maxsize=1)
def get_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(relative: str) -> Path:
    """Resolves a path from config.yaml (given relative to the repo root)."""
    return (REPO_ROOT / relative).resolve()
