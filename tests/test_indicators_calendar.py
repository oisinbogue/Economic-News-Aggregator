"""Tests indicators/calendar.py's release_schedule rule grammar and forward
date computation (INDICATORS_BRIEF.md §8/item 1: "hand-checked fixtures
including month-boundary and DST-transition cases"), plus
`populate_releases()`'s db-facing behaviour.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import indicators.calendar as calendar_mod
import indicators.config as config_mod
import indicators.db as db_mod
from indicators.calendar import RuleError, compute_next_release, parse_rule, populate_releases


# --------------------------------------------------------------------------
# parse_rule
# --------------------------------------------------------------------------

def test_parse_daily_rule():
    parsed = parse_rule("daily, 21:00 Europe/London")
    assert parsed["kind"] == "daily"
    assert parsed["hour"] == 21
    assert parsed["minute"] == 0
    assert parsed["tz"] == ZoneInfo("Europe/London")


def test_parse_monthly_nth_weekday_rule():
    parsed = parse_rule("monthly first friday, 08:30 America/New_York")
    assert parsed["kind"] == "monthly_nth_weekday"
    assert parsed["nth"] == "first"
    assert parsed["weekday"] == 4  # Friday, 0=Monday


def test_parse_quarterly_lag_rule():
    parsed = parse_rule("quarterly lag 45d, 08:30 America/New_York")
    assert parsed["kind"] == "quarterly_lag"
    assert parsed["lag_days"] == 45


def test_parse_rejects_unknown_shape():
    with pytest.raises(RuleError):
        parse_rule("whenever we feel like it, 09:00 UTC")


def test_parse_rejects_unknown_weekday():
    with pytest.raises(RuleError):
        parse_rule("weekly funday, 09:00 UTC")


def test_parse_rejects_unknown_timezone():
    with pytest.raises(RuleError):
        parse_rule("daily, 09:00 Not/AZone")


def test_parse_rejects_bad_time():
    with pytest.raises(RuleError):
        parse_rule("daily, 25:00 UTC")


# --------------------------------------------------------------------------
# Fixed-calendar rules
# --------------------------------------------------------------------------

def test_daily_next_occurrence_same_day_before_time():
    after = datetime(2026, 3, 10, 6, 0, tzinfo=timezone.utc)
    scheduled_at, tz = compute_next_release("daily, 21:00 UTC", after)
    assert scheduled_at == datetime(2026, 3, 10, 21, 0, tzinfo=timezone.utc)
    assert tz == "UTC"


def test_daily_next_occurrence_rolls_to_next_day_after_time():
    after = datetime(2026, 3, 10, 22, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("daily, 21:00 UTC", after)
    assert scheduled_at == datetime(2026, 3, 11, 21, 0, tzinfo=timezone.utc)


def test_monthly_nth_weekday_first_friday():
    # August 2026: Aug 1 is a Saturday, so the first Friday is Aug 7.
    after = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("monthly first friday, 08:30 America/New_York", after)
    local = scheduled_at.astimezone(ZoneInfo("America/New_York"))
    assert local.date() == date(2026, 8, 7)
    assert local.weekday() == 4


def test_monthly_nth_weekday_rolls_to_next_month_once_passed():
    # First Friday of August 2026 is Aug 7; asking from Aug 10 should land
    # on September's first Friday (Sep 4), not repeat August's.
    after = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("monthly first friday, 08:30 America/New_York", after)
    local = scheduled_at.astimezone(ZoneInfo("America/New_York"))
    assert local.date() == date(2026, 9, 4)


def test_monthly_last_weekday():
    # Last Wednesday of February 2026 (non-leap year, 28 days): Feb 25.
    after = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("monthly last wednesday, 09:00 UTC", after)
    assert scheduled_at.date() == date(2026, 2, 25)


def test_monthly_day_clamped_to_shorter_month():
    # "monthly day 31" in a 30-day month clamps to the month's last day.
    after = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("monthly day 31, 09:00 UTC", after)
    assert scheduled_at.date() == date(2026, 4, 30)


def test_monthly_day_rolls_year_boundary():
    after = datetime(2026, 12, 20, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("monthly day 15, 09:00 UTC", after)
    assert scheduled_at.date() == date(2027, 1, 15)


def test_annual_month_day():
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("annual month 4 day 15, 06:00 UTC", after)
    assert scheduled_at.date() == date(2026, 4, 15)


# --------------------------------------------------------------------------
# DST transitions (Europe/London: clocks go forward the last Sunday of
# March, back the last Sunday of October)
# --------------------------------------------------------------------------

def test_daily_rule_across_spring_forward():
    # 2026-03-29 is the UK's spring-forward Sunday (01:00 UTC -> 02:00 BST).
    # A 07:00 Europe/London release the morning after should still land at
    # 07:00 *local* time, i.e. 06:00 UTC (BST is UTC+1), not 07:00 UTC.
    after = datetime(2026, 3, 29, 5, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("daily, 07:00 Europe/London", after)
    assert scheduled_at == datetime(2026, 3, 29, 6, 0, tzinfo=timezone.utc)
    local = scheduled_at.astimezone(ZoneInfo("Europe/London"))
    assert (local.hour, local.minute) == (7, 0)


def test_daily_rule_across_autumn_back():
    # 2026-10-25 is the UK's clocks-back Sunday (BST -> GMT). A 07:00
    # Europe/London release should land at 07:00 UTC that day (GMT = UTC).
    after = datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("daily, 07:00 Europe/London", after)
    assert scheduled_at == datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Lag rules
# --------------------------------------------------------------------------

def test_monthly_lag_anchored_on_prior_period_end():
    # Prior observation's period_end is Jan 31 2026 -> next period is
    # February, period_end Feb 28 2026 (non-leap), release 15 days later.
    after = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release(
        "monthly lag 15d, 08:30 America/New_York", after, period_end=date(2026, 1, 31)
    )
    local = scheduled_at.astimezone(ZoneInfo("America/New_York"))
    assert local.date() == date(2026, 3, 15)


def test_monthly_lag_leap_year_february():
    # Prior period_end Jan 31 2028 -> next period Feb 2028 (leap year, 29
    # days) -> period_end Feb 29 2028, +10 days = Mar 10 2028.
    after = datetime(2028, 2, 1, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release(
        "monthly lag 10d, 09:00 UTC", after, period_end=date(2028, 1, 31)
    )
    assert scheduled_at.date() == date(2028, 3, 10)


def test_quarterly_lag_anchored_on_prior_period_end():
    # Prior period_end Dec 31 2025 (Q4) -> next period Q1 2026, period_end
    # Mar 31 2026, +45 days.
    after = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release(
        "quarterly lag 45d, 08:30 America/New_York", after, period_end=date(2025, 12, 31)
    )
    local = scheduled_at.astimezone(ZoneInfo("America/New_York"))
    assert local.date() == date(2026, 5, 15)


def test_annual_lag_anchored_on_prior_period_end():
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release(
        "annual lag 60d, 09:00 UTC", after, period_end=date(2025, 12, 31)
    )
    assert scheduled_at.date() == date(2027, 3, 1)  # Dec 31 2026 + 60 days


def test_lag_rule_without_period_end_anchors_on_after():
    # No prior observation: anchor on the period containing `after` itself
    # (Feb 2026), so next release covers Feb -> period_end Feb 28 + 15d.
    after = datetime(2026, 2, 10, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release("monthly lag 15d, 09:00 UTC", after, period_end=None)
    assert scheduled_at.date() == date(2026, 3, 15)


def test_lag_rule_skips_ahead_when_lag_already_passed():
    # Prior period_end Jan 31 2026 -> naive next release Feb 28 + 15d =
    # Mar 15, but `after` is already Apr 1 -> must roll to the *next*
    # period (March -> period_end Mar 31, +15d = Apr 15), not return a
    # release date in the past.
    after = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
    scheduled_at, _ = compute_next_release(
        "monthly lag 15d, 09:00 UTC", after, period_end=date(2026, 1, 31)
    )
    assert scheduled_at.date() == date(2026, 4, 15)
    assert scheduled_at > after


def test_compute_next_release_requires_aware_after():
    with pytest.raises(ValueError):
        compute_next_release("daily, 09:00 UTC", datetime(2026, 1, 1))


# --------------------------------------------------------------------------
# populate_releases (db-facing)
# --------------------------------------------------------------------------

def _write_yaml(path, data):
    import yaml
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def indicators_env(tmp_path, monkeypatch):
    indicators_dir = tmp_path / "indicators"
    series_dir = indicators_dir / "series"
    series_dir.mkdir(parents=True)

    _write_yaml(indicators_dir / "countries.yaml", {
        "countries": {"us": {"iso": "us", "name": "United States", "currency": "USD", "timezone": "UTC"}}
    })
    _write_yaml(indicators_dir / "core.yaml", {
        "indicators": {
            "cpi_headline": {
                "topic": "prices", "unit": "percent", "frequency": "M",
                "transform_default": "yoy", "direction_good": "neutral",
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
            "cpi_headline": {
                "provider": "dbnomics", "provider_series_id": "BLS/cu/CUSR0000SA0",
                "fallback_providers": [], "source_agency": "BLS",
                "source_url": "https://example.com", "licence": "public-domain",
                "seasonal_adjustment": "sa", "revisable": True, "news_topic_tags": [],
                "release_schedule": "monthly lag 15d, 08:30 America/New_York",
                "release_lag_days": 15,
            },
            "policy_rate": {
                "provider": "dbnomics", "provider_series_id": "FED/H15/RIFSPFF_N.B",
                "fallback_providers": [], "source_agency": "Fed",
                "source_url": "https://example.com", "licence": "public-domain",
                "seasonal_adjustment": "nsa", "revisable": False, "news_topic_tags": [],
                # no release_schedule -- irregular (FOMC meeting calendar)
            },
        },
        "extras": [],
    })

    root_config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "data" / "indicators.db"
    _write_yaml(root_config_path, {
        "paths": {"indicators_db_file": str(db_path)},
        "indicators": {"dbnomics_base_url": "https://api.db.nomics.world/v22/"},
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


def test_populate_releases_only_touches_series_with_a_schedule(indicators_env):
    with db_mod.get_connection() as conn:
        inserted = populate_releases(conn, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        rows = conn.execute("SELECT series_id FROM releases").fetchall()

    assert inserted == 1
    assert {r["series_id"] for r in rows} == {"us.cpi_headline"}


def test_populate_releases_is_idempotent(indicators_env):
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with db_mod.get_connection() as conn:
        first = populate_releases(conn, now=now)
        second = populate_releases(conn, now=now)
        count = conn.execute("SELECT COUNT(*) AS n FROM releases").fetchone()["n"]

    assert first == 1
    assert second == 0  # already has a future 'scheduled' row -> no duplicate
    assert count == 1


def test_populate_releases_marks_missed_after_grace_with_no_new_observation(indicators_env):
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with db_mod.get_connection() as conn:
        populate_releases(conn, now=now)
        row = conn.execute("SELECT scheduled_at FROM releases WHERE series_id = 'us.cpi_headline'").fetchone()
        scheduled_at = datetime.fromisoformat(row["scheduled_at"])

        far_future = scheduled_at + calendar_mod.MISSED_GRACE + calendar_mod.MISSED_GRACE
        populate_releases(conn, now=far_future)
        statuses = [r["status"] for r in conn.execute("SELECT status FROM releases WHERE series_id = 'us.cpi_headline'")]

    assert "missed" in statuses
    # a fresh 'scheduled' row should also have been added for the next occurrence
    assert "scheduled" in statuses


def test_populate_releases_marks_released_when_observation_lands_after_schedule(indicators_env):
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with db_mod.get_connection() as conn:
        populate_releases(conn, now=now)
        row = conn.execute("SELECT scheduled_at FROM releases WHERE series_id = 'us.cpi_headline'").fetchone()
        scheduled_at = datetime.fromisoformat(row["scheduled_at"])

        conn.execute(
            """
            INSERT INTO observations (series_id, period_start, period_end, frequency, value, fetched_at)
            VALUES ('us.cpi_headline', '2026-02-01', '2026-02-28', 'M', 3.1, ?)
            """,
            ((scheduled_at + calendar_mod.timedelta(hours=1)).isoformat(),),
        )

        after_release = scheduled_at + calendar_mod.timedelta(days=1)
        populate_releases(conn, now=after_release)
        row = conn.execute(
            "SELECT status, actual_at FROM releases WHERE series_id = 'us.cpi_headline' ORDER BY id LIMIT 1"
        ).fetchone()

    assert row["status"] == "released"
    assert row["actual_at"] is not None
