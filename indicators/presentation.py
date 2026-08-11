"""Display formatting for indicator cards -- HTML-only concerns that must
never leak into indicators.export's manifest.json (that file is the API;
this module is what indicators.build layers on top of a manifest entry
before handing it to a Jinja template).

Kept separate from indicators/build.py because it's pure functions over
plain dicts/strings with no DB or Jinja dependency, which makes it easy to
unit test the formatting rules in isolation (see tests/test_indicators_*).
"""

from __future__ import annotations

from datetime import datetime, timezone

# Flag emoji for the Phase-1 countries/blocs. EU has no ISO flag of its own
# (blue banner with stars is a stand-alone Unicode char), and anything not
# listed here falls back to its plain ISO code in the template rather than
# guessing -- wrong flags are worse than no flag.
COUNTRY_FLAGS = {
    "us": "\U0001F1FA\U0001F1F8",
    "gb": "\U0001F1EC\U0001F1E7",
    "ie": "\U0001F1EE\U0001F1EA",
    "eu": "\U0001F1EA\U0001F1FA",
    "de": "\U0001F1E9\U0001F1EA",
}

# Every unit the `series` table allows (indicators/db.py SCHEMA), mapped to
# how many decimal places its headline value/change should render at.
UNIT_DECIMALS = {
    "percent": 2,
    "percent_gdp": 1,
    "bps": 0,
    "index": 1,
    "ratio": 4,
    "count": 0,
    "currency": 2,
}

SCALE_SUFFIX = {
    "units": "",
    "thousands": "k",
    "millions": "M",
    "billions": "B",
    "trillions": "T",
}

MONTH_ABBR = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def country_flag(iso: str) -> str:
    return COUNTRY_FLAGS.get(iso, "")


def format_number(value: float, unit: str, scale: str = "units") -> str:
    """Renders a bare number per its unit's convention -- no sign, no unit
    suffix. `value_display`/`change_display` below wrap this with the parts
    that differ between a headline value and a +/- change.
    """
    decimals = UNIT_DECIMALS.get(unit, 2)
    suffix = SCALE_SUFFIX.get(scale, "") if unit == "currency" else ""
    if unit == "count":
        return f"{value:,.0f}{suffix}"
    return f"{value:,.{decimals}f}{suffix}"


def value_display(value: float | None, unit: str, unit_currency: str | None, scale: str = "units") -> str:
    """Headline card value, e.g. '2.10%', '4.4% GDP', '128 bps', '$411.2B'."""
    if value is None:
        return "—"  # em dash -- "no data yet", distinct from a real 0
    number = format_number(value, unit, scale)
    if unit == "percent":
        return f"{number}%"
    if unit == "percent_gdp":
        return f"{number}% GDP"
    if unit == "bps":
        return f"{number} bps"
    if unit == "currency":
        symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(unit_currency or "", "")
        return f"{symbol}{number}" if symbol else f"{number} {unit_currency or ''}".strip()
    return number


def change_display(change: float | None, unit: str) -> str | None:
    """'+0.40pp' for a percent-family unit (the change is a percentage-POINT
    move, not a second percentage, so it must never render with a trailing
    '%' -- that would silently claim precision the underlying number
    doesn't have), '+128 bps', '+0.0120' for a ratio, etc.
    """
    if change is None:
        return None
    sign = "+" if change > 0 else ("−" if change < 0 else "±")
    number = format_number(abs(change), unit if unit not in ("percent", "percent_gdp") else "index")
    if unit in ("percent", "percent_gdp"):
        return f"{sign}{number}pp"
    if unit == "bps":
        return f"{sign}{number} bps"
    return f"{sign}{number}"


def trend_meta(card: dict) -> dict:
    """Colour + arrow direction for a card's change badge, driven by
    `direction_good` rather than the raw sign of the change -- see
    INDICATORS_BRIEF.md #9 "Colour semantics".

    Returns {"color": "good"|"bad"|"warn"|"neutral", "arrow": "up"|"down"|"flat"}.
    The arrow and colour are computed together (never colour alone carries
    the meaning): for an `up`/`down` series the arrow reflects the value's
    actual movement and the colour reflects whether that movement is
    favourable; for a `neutral` series with a target band the arrow reflects
    the value's position relative to the band (above/below/inside) and the
    colour reflects the same judgement, so the two never disagree.
    """
    direction_good = card["direction_good"]
    change = card["change_vs_prior"]

    if direction_good in ("up", "down"):
        if change is None or change == 0:
            return {"color": "neutral", "arrow": "flat"}
        rising = change > 0
        favourable = rising if direction_good == "up" else not rising
        return {"color": "good" if favourable else "bad", "arrow": "up" if rising else "down"}

    # direction_good == "neutral"
    target_low = card.get("target_band_low")
    target_high = card.get("target_band_high")
    latest = card.get("latest_value")
    if target_low is not None and target_high is not None and latest is not None:
        if latest > target_high:
            return {"color": "bad", "arrow": "up"}
        if latest < target_low:
            return {"color": "warn", "arrow": "down"}
        return {"color": "good", "arrow": "flat"}

    # No target band to judge against -- never colour a neutral series by
    # the raw sign of its change, but the arrow still shows which way it
    # moved (direction is still useful information, just not "good/bad").
    if change is None or change == 0:
        return {"color": "neutral", "arrow": "flat"}
    return {"color": "neutral", "arrow": "up" if change > 0 else "down"}


def period_label(period_start: str | None, period_end: str | None, frequency: str) -> str | None:
    """Human label for a reference period, e.g. 'Q1 2026', 'Aug 2025',
    '2025', '11 Aug 2026' -- kept distinct from any release/publication date
    per the brief's "reference period vs release date" rule (#5).
    """
    if not period_start:
        return None
    try:
        start = datetime.fromisoformat(period_start)
    except ValueError:
        return period_start

    if frequency == "A":
        return str(start.year)
    if frequency == "Q":
        quarter = (start.month - 1) // 3 + 1
        return f"Q{quarter} {start.year}"
    if frequency == "M":
        return f"{MONTH_ABBR[start.month - 1]} {start.year}"
    if frequency == "W":
        return f"Week of {start.day} {MONTH_ABBR[start.month - 1]} {start.year}"
    # Daily / anything else: a single calendar date.
    return f"{start.day} {MONTH_ABBR[start.month - 1]} {start.year}"


def next_release_label(scheduled_at: str | None, now: datetime | None = None) -> str | None:
    """'18 Aug · in 11 days' / '18 Aug · today' / '18 Aug · 3 days ago'.

    A release in the past still renders (rather than being hidden) because
    `releases.status = 'scheduled'` rows aren't retired the instant the
    clock passes them -- Phase 3's calendar.py owns marking them 'actual',
    and until then a slightly-overdue release is worth showing, not hiding.
    """
    if not scheduled_at:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        when = datetime.fromisoformat(scheduled_at)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    date_part = f"{when.day} {MONTH_ABBR[when.month - 1]}"
    days = (when.date() - now.date()).days
    if days == 0:
        relative = "today"
    elif days > 0:
        relative = f"in {days} day{'s' if days != 1 else ''}"
    else:
        relative = f"{-days} day{'s' if days != -1 else ''} ago"
    return f"{date_part} · {relative}"


def enrich_card(card: dict, now: datetime | None = None, country_name: str | None = None) -> dict:
    """Adds display-only fields to a manifest entry for template consumption.
    Leaves every existing key untouched -- this is additive so `card` still
    satisfies anything written against build_manifest_entry's shape.
    """
    trend = trend_meta(card)
    enriched = dict(card)
    enriched["flag"] = country_flag(card["country"])
    enriched["country_name"] = country_name or card["country"].upper()
    enriched["value_display"] = value_display(
        card["latest_value"], card["unit"], card.get("unit_currency"), card.get("scale", "units")
    )
    enriched["change_display"] = change_display(card["change_vs_prior"], card["unit"])
    enriched["trend_color"] = trend["color"]
    enriched["trend_arrow"] = trend["arrow"]
    enriched["period_label"] = period_label(card["period_start"], card["period_end"], card["frequency"])
    enriched["next_release_label"] = next_release_label(card.get("next_release"), now)
    return enriched
