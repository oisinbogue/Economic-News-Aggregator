"""Shared UTC time helpers.

The whole DB stores UTC ISO8601 timestamps, so anything that resolves
"today" must use UTC too. `date.today()` / `datetime.now()` use the local
system clock, which silently drifts from the DB's timestamps near local
midnight (masked in CI, where the GitHub Actions runner is UTC).
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_today() -> date:
    return utc_now().date()
