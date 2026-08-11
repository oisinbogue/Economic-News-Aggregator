"""Provider interface for the indicators subsystem.

Every provider module under `indicators/providers/` (dbnomics.py, and
later ecb.py/stooq.py/etc.) implements this `Provider` protocol: given a
`series` table row (as a dict, e.g. from `indicators.db`), fetch and return
every observation currently available for it. `indicators/fetch.py`
dispatches to the right module by `series.provider` and writes the
returned `Observation`s into the `observations` table -- see
INDICATORS_SCHEMA_PROPOSAL.md §3 for that table's shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass
class Observation:
    period_start: date
    period_end: date
    value: float
    is_preliminary: bool = False


class Provider(Protocol):
    async def fetch(self, series_spec: dict) -> list[Observation]: ...
