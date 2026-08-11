"""Release-schedule rule grammar + forward date computation for the
indicators subsystem.

INDICATORS_BRIEF.md §8 leaves `series.release_schedule`'s grammar
deliberately undefined ("Choose the specific schedule yourself") -- this
module defines it, and is the sole place that parses or computes against
it. See INDICATORS.md for the human-facing grammar reference; the EBNF-ish
summary is repeated here since it's the thing this module's regexes must
stay in sync with:

    rule       := schedule "," ws time
    schedule   := "daily"
                | "weekly " weekday
                | "monthly " nth " " weekday
                | "monthly day " day
                | "monthly lag " lag_days "d"
                | "quarterly lag " lag_days "d"
                | "annual lag " lag_days "d"
                | "annual month " month " day " day
    time       := HH ":" MM " " iana_tz
    nth        := "first" | "second" | "third" | "fourth" | "last"
    weekday    := "monday" | "tuesday" | ... | "sunday"
    day        := 1-31 | "last"
    month      := 1-12
    lag_days   := non-negative integer

Two families, computed differently:

  - **Fixed-calendar** rules (daily/weekly/monthly-nth-weekday/monthly-day/
    annual-month-day) recur on their own clock, independent of any
    series data -- e.g. the US jobs report is always "first Friday of the
    month" regardless of what the prior report said. `_next_fixed_calendar()`
    walks forward from `after` until it finds an occurrence strictly after it.

  - **Lag** rules (monthly/quarterly/annual lag Nd) are anchored to the
    series' own *reference period*: "N days after period end". Computing
    the next one therefore needs to know the period the next release will
    cover, which `populate_releases()` derives from the series' latest
    stored observation (or, if there isn't one yet, from `after` itself) --
    see `_next_lag_release()`.

Every scheduled_at this module produces is stored as a UTC ISO datetime in
`releases.scheduled_at`, with `releases.scheduled_tz` holding the rule's own
IANA zone so the UI can convert back to local display time (brief §8: "Store
release times in UTC with an IANA timezone attached; render in the
visitor's local time").

Where a provider exposes real release-date metadata, prefer that over the
rule string (brief §8) -- none of Phase 1's providers (dbnomics/ecb/
eurostat/ons/boe/stooq) expose one (see each module's docstring on the
missing revision/preliminary flag, the same underlying gap), so
`populate_releases()` only has the rule-string path to fall back on for
now; a provider that starts exposing one is a future per-provider override,
not a change to this module's contract.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NTH_INDEX = {"first": 0, "second": 1, "third": 2, "fourth": 3}

_TIME_RE = r"(?P<hour>\d{2}):(?P<minute>\d{2}) (?P<tz>[A-Za-z0-9_+\-/]+)$"

_PATTERNS = [
    ("daily", re.compile(rf"^daily,\s*{_TIME_RE}")),
    ("weekly", re.compile(rf"^weekly (?P<weekday>\w+),\s*{_TIME_RE}")),
    ("monthly_nth_weekday", re.compile(rf"^monthly (?P<nth>first|second|third|fourth|last) (?P<weekday>\w+),\s*{_TIME_RE}")),
    ("monthly_day", re.compile(rf"^monthly day (?P<day>\w+),\s*{_TIME_RE}")),
    ("monthly_lag", re.compile(rf"^monthly lag (?P<lag>\d+)d,\s*{_TIME_RE}")),
    ("quarterly_lag", re.compile(rf"^quarterly lag (?P<lag>\d+)d,\s*{_TIME_RE}")),
    ("annual_lag", re.compile(rf"^annual lag (?P<lag>\d+)d,\s*{_TIME_RE}")),
    ("annual_month_day", re.compile(rf"^annual month (?P<month>\d{{1,2}}) day (?P<day>\w+),\s*{_TIME_RE}")),
]


class RuleError(ValueError):
    """Raised for a release_schedule string that doesn't match the grammar."""


def parse_rule(rule: str) -> dict:
    """Parses a release_schedule string into a dict, or raises RuleError.

    Callers get back {"kind": ..., "hour": int, "minute": int, "tz":
    ZoneInfo, ...kind-specific fields}. Validates the weekday/nth/tz/day
    values eagerly (rather than deferring to a KeyError later) so a typo
    in config is caught by `python -m indicators.config` alongside every
    other config problem, not by a fetch/build run crashing mid-way.
    """
    rule = rule.strip()
    for kind, pattern in _PATTERNS:
        match = pattern.match(rule)
        if not match:
            continue
        fields = match.groupdict()
        try:
            tz = ZoneInfo(fields["tz"])
        except Exception as exc:
            raise RuleError(f"unknown timezone {fields['tz']!r} in rule {rule!r}") from exc

        parsed = {
            "kind": kind,
            "hour": int(fields["hour"]),
            "minute": int(fields["minute"]),
            "tz": tz,
        }
        if not (0 <= parsed["hour"] <= 23 and 0 <= parsed["minute"] <= 59):
            raise RuleError(f"invalid time of day in rule {rule!r}")

        if kind in ("weekly", "monthly_nth_weekday"):
            weekday = fields["weekday"].lower()
            if weekday not in WEEKDAYS:
                raise RuleError(f"unknown weekday {weekday!r} in rule {rule!r}")
            parsed["weekday"] = WEEKDAYS.index(weekday)

        if kind == "monthly_nth_weekday":
            nth = fields["nth"].lower()
            if nth not in NTH_INDEX and nth != "last":
                raise RuleError(f"unknown ordinal {nth!r} in rule {rule!r}")
            parsed["nth"] = nth

        if kind in ("monthly_day", "annual_month_day"):
            day = fields["day"].lower()
            if day != "last":
                if not day.isdigit() or not (1 <= int(day) <= 31):
                    raise RuleError(f"invalid day {fields['day']!r} in rule {rule!r}")
                day = int(fields["day"])
            parsed["day"] = day

        if kind == "annual_month_day":
            month = int(fields["month"])
            if not (1 <= month <= 12):
                raise RuleError(f"invalid month {month!r} in rule {rule!r}")
            parsed["month"] = month

        if kind in ("monthly_lag", "quarterly_lag", "annual_lag"):
            parsed["lag_days"] = int(fields["lag"])

        return parsed

    raise RuleError(f"release_schedule does not match any known rule shape: {rule!r}")


# --------------------------------------------------------------------------
# Calendar arithmetic
# --------------------------------------------------------------------------

def _add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def _month_end(year: int, month: int) -> date:
    return _add_months(date(year, month, 1), 1) - timedelta(days=1)


def _quarter_end(d: date) -> date:
    quarter_end_month = ((d.month - 1) // 3 + 1) * 3
    return _month_end(d.year, quarter_end_month)


def _year_end(year: int) -> date:
    return date(year, 12, 31)


def _clamp_day(year: int, month: int, day: int | str) -> date:
    last = _month_end(year, month).day
    if day == "last":
        return date(year, month, last)
    return date(year, month, min(int(day), last))


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: str) -> date:
    """`nth` is one of first/second/third/fourth/last; `weekday` is 0=Monday."""
    first_of_month = date(year, month, 1)
    first_weekday_offset = (weekday - first_of_month.weekday()) % 7
    first_occurrence = first_of_month + timedelta(days=first_weekday_offset)
    if nth == "last":
        candidate = first_occurrence
        while True:
            next_candidate = candidate + timedelta(days=7)
            if next_candidate.month != month:
                return candidate
            candidate = next_candidate
    return first_occurrence + timedelta(weeks=NTH_INDEX[nth])


def _at_time(d: date, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)


# --------------------------------------------------------------------------
# Next-occurrence computation
# --------------------------------------------------------------------------

def _next_fixed_calendar(parsed: dict, after: datetime) -> datetime:
    """Walks forward month-by-month (or day/week for daily/weekly rules)
    until it finds an occurrence of `parsed`'s schedule strictly after
    `after`. `after` is converted into the rule's own tz first so "today"
    is evaluated in the rule's local calendar, not the caller's.
    """
    local_after = after.astimezone(parsed["tz"])
    kind = parsed["kind"]
    hour, minute, tz = parsed["hour"], parsed["minute"], parsed["tz"]

    if kind == "daily":
        candidate_date = local_after.date()
        candidate = _at_time(candidate_date, hour, minute, tz)
        if candidate <= local_after:
            candidate = _at_time(candidate_date + timedelta(days=1), hour, minute, tz)
        return candidate

    if kind == "weekly":
        candidate_date = local_after.date()
        offset = (parsed["weekday"] - candidate_date.weekday()) % 7
        candidate = _at_time(candidate_date + timedelta(days=offset), hour, minute, tz)
        if candidate <= local_after:
            candidate = _at_time(candidate_date + timedelta(days=offset + 7), hour, minute, tz)
        return candidate

    if kind == "monthly_nth_weekday":
        year, month = local_after.year, local_after.month
        for _ in range(14):  # generous bound; "last" + short months never need more than 2-3 hops
            occurrence_date = _nth_weekday_of_month(year, month, parsed["weekday"], parsed["nth"])
            candidate = _at_time(occurrence_date, hour, minute, tz)
            if candidate > local_after:
                return candidate
            year, month = divmod(year * 12 + month, 12)
            month += 1
        raise RuleError("could not resolve monthly_nth_weekday rule")  # pragma: no cover -- defensive only

    if kind == "monthly_day":
        year, month = local_after.year, local_after.month
        for _ in range(14):
            occurrence_date = _clamp_day(year, month, parsed["day"])
            candidate = _at_time(occurrence_date, hour, minute, tz)
            if candidate > local_after:
                return candidate
            year, month = divmod(year * 12 + month, 12)
            month += 1
        raise RuleError("could not resolve monthly_day rule")  # pragma: no cover -- defensive only

    if kind == "annual_month_day":
        year = local_after.year
        for _ in range(3):
            occurrence_date = _clamp_day(year, parsed["month"], parsed["day"])
            candidate = _at_time(occurrence_date, hour, minute, tz)
            if candidate > local_after:
                return candidate
            year += 1
        raise RuleError("could not resolve annual_month_day rule")  # pragma: no cover -- defensive only

    raise RuleError(f"not a fixed-calendar rule kind: {kind!r}")  # pragma: no cover -- defensive only


def _next_period_end(kind: str, from_period_end: date) -> date:
    """The next period-end boundary strictly after `from_period_end`, for a
    lag rule's frequency family."""
    if kind == "monthly_lag":
        next_month_start = _add_months(date(from_period_end.year, from_period_end.month, 1), 1)
        return _month_end(next_month_start.year, next_month_start.month)
    if kind == "quarterly_lag":
        return _quarter_end(_quarter_end(from_period_end) + timedelta(days=1))
    if kind == "annual_lag":
        return _year_end(from_period_end.year + 1)
    raise RuleError(f"not a lag rule kind: {kind!r}")  # pragma: no cover -- defensive only


def _current_period_end(kind: str, on: date) -> date:
    """The period-end boundary that `on` itself falls inside (used when no
    prior observation exists to anchor the lag computation on)."""
    if kind == "monthly_lag":
        return _month_end(on.year, on.month)
    if kind == "quarterly_lag":
        return _quarter_end(on)
    if kind == "annual_lag":
        return _year_end(on.year)
    raise RuleError(f"not a lag rule kind: {kind!r}")  # pragma: no cover -- defensive only


def _next_lag_release(parsed: dict, after: datetime, period_end: date | None) -> datetime:
    """Next lag-rule release strictly after `after`.

    Anchored on `period_end` (the series' latest known observation's
    period_end) when given -- the next release covers the period
    immediately following it. Without one (a brand-new series with no
    observations yet), anchors on the period containing `after` itself,
    which is the best available guess for "when does this series' next
    print land" before any data has been fetched.
    """
    kind = parsed["kind"]
    tz = parsed["tz"]
    local_after_date = after.astimezone(tz).date()

    if period_end is not None:
        # A known observation exists: the next release covers the period
        # immediately *after* it.
        candidate_period_end = _next_period_end(kind, period_end)
    else:
        # No observation yet: guess the period containing `after` itself
        # hasn't been released yet (the common case -- most of a period is
        # still in progress when there's nothing stored for it at all).
        candidate_period_end = _current_period_end(kind, local_after_date)

    while True:
        release_date = candidate_period_end + timedelta(days=parsed["lag_days"])
        candidate = _at_time(release_date, parsed["hour"], parsed["minute"], tz)
        if candidate > after:
            return candidate
        candidate_period_end = _next_period_end(kind, candidate_period_end)


def compute_next_release(rule: str, after: datetime, period_end: date | None = None) -> tuple[datetime, str]:
    """Computes the next release strictly after `after` for a release_schedule
    rule string.

    `after` must be timezone-aware (UTC is the normal case -- this is what
    `populate_releases()` passes). `period_end` is the series' latest
    stored observation's period_end, only consulted for lag-family rules
    (see `_next_lag_release`); ignored for fixed-calendar rules.

    Returns (scheduled_at_utc, tz_name) -- `scheduled_at_utc` always has
    tzinfo=UTC (brief §8: "Store release times in UTC"), `tz_name` is the
    rule's own IANA zone string (brief §8: "with an IANA timezone attached").
    """
    if after.tzinfo is None:
        raise ValueError("compute_next_release requires a timezone-aware `after`")

    parsed = parse_rule(rule)
    if parsed["kind"] in ("monthly_lag", "quarterly_lag", "annual_lag"):
        local_next = _next_lag_release(parsed, after, period_end)
    else:
        local_next = _next_fixed_calendar(parsed, after)

    return local_next.astimezone(timezone.utc), str(parsed["tz"])


# --------------------------------------------------------------------------
# releases table population
# --------------------------------------------------------------------------

# How long past a scheduled release with no matching new observation before
# it's given up on and marked 'missed' rather than left 'scheduled'
# forever. Generous relative to release_lag_days error margins (a rule is a
# best-effort encoding of a real-world calendar this project doesn't have
# authoritative access to -- see this module's docstring on preferring
# provider metadata where available) -- a rule that's a few days off
# shouldn't spuriously flip every release to 'missed'.
MISSED_GRACE = timedelta(days=5)


def _latest_period_end(conn, series_id: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(period_end) AS period_end FROM observations WHERE series_id = ?",
        (series_id,),
    ).fetchone()
    if row is None or row["period_end"] is None:
        return None
    return date.fromisoformat(row["period_end"])


def _reconcile_past_releases(conn, series_id: str, now: datetime) -> None:
    """Resolves any 'scheduled' release row whose time has passed: 'released'
    if an observation was fetched on or after it, 'missed' once MISSED_GRACE
    has elapsed with nothing new, otherwise left 'scheduled' (still within
    grace -- the print may just be running late)."""
    due_rows = conn.execute(
        """
        SELECT id, scheduled_at FROM releases
        WHERE series_id = ? AND status = 'scheduled' AND scheduled_at <= ?
        """,
        (series_id, now.isoformat()),
    ).fetchall()

    for row in due_rows:
        scheduled_at = row["scheduled_at"]
        matched = conn.execute(
            """
            SELECT MIN(fetched_at) AS fetched_at FROM observations
            WHERE series_id = ? AND fetched_at >= ?
            """,
            (series_id, scheduled_at),
        ).fetchone()
        if matched and matched["fetched_at"]:
            conn.execute(
                "UPDATE releases SET status = 'released', actual_at = ? WHERE id = ?",
                (matched["fetched_at"], row["id"]),
            )
        elif now - datetime.fromisoformat(scheduled_at) > MISSED_GRACE:
            conn.execute("UPDATE releases SET status = 'missed' WHERE id = ?", (row["id"],))


def populate_releases(conn, now: datetime | None = None) -> int:
    """Reconciles past releases and ensures every active series with a
    release_schedule has at least one upcoming 'scheduled' releases row.

    Safe to call every run (idempotent): a series that already has a future
    'scheduled' row is left alone rather than gaining a duplicate. Series
    with no release_schedule (still most of them -- see INDICATORS.md) are
    skipped entirely, not an error.

    Returns the number of new release rows inserted.
    """
    now = now or datetime.now(timezone.utc)

    series_rows = conn.execute(
        "SELECT series_id, release_schedule FROM series WHERE active = 1 AND release_schedule IS NOT NULL"
    ).fetchall()

    inserted = 0
    for row in series_rows:
        series_id, rule = row["series_id"], row["release_schedule"]
        _reconcile_past_releases(conn, series_id, now)

        upcoming = conn.execute(
            """
            SELECT 1 FROM releases
            WHERE series_id = ? AND status = 'scheduled' AND scheduled_at > ?
            LIMIT 1
            """,
            (series_id, now.isoformat()),
        ).fetchone()
        if upcoming is not None:
            continue

        try:
            period_end = _latest_period_end(conn, series_id)
            scheduled_at, tz_name = compute_next_release(rule, now, period_end)
        except RuleError as exc:
            print(f"calendar: skipping {series_id!r}: {exc}")
            continue

        conn.execute(
            "INSERT INTO releases (series_id, scheduled_at, scheduled_tz, status) VALUES (?, ?, ?, 'scheduled')",
            (series_id, scheduled_at.isoformat(), tz_name),
        )
        inserted += 1

    return inserted


def main() -> None:
    from indicators.db import get_connection, init_db

    init_db()
    with get_connection() as conn:
        n = populate_releases(conn)
    print(f"populate_releases: inserted {n} new release row(s)")


if __name__ == "__main__":
    main()
