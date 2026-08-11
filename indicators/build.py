"""Static site generator for the indicators subsystem.

Renders `data/indicators.db` into the HTML routes under
`<site.output_dir>/indicators/`:

    indicators/index.html                     the all-country grid
    indicators/<country>/index.html           one country's indicators
    indicators/<country>/<indicator_key>/index.html   one series' page

Mirrors pipeline/build.py exactly where it matters -- same Jinja2
Environment (FileSystemLoader over templates/, select_autoescape,
trim_blocks/lstrip_blocks), same `site` meta dict, and the same shared
kwargs every template gets (`asset_prefix`, `active_nav`, `canonical_path`,
`pagefind_index`) so templates/base.html works here unchanged.

`asset_prefix` is the relative depth back to the site root, which differs
per route: the grid is one level down ("../"), a country page two
("../../"), a series page three ("../../../").

Card data comes from `indicators.export.build_manifest_entry` rather than
a second, parallel set of queries -- the same reason pipeline/export.py
reuses pipeline/build.py's helpers: "what a card shows" must not drift
between the HTML and the JSON the grid fetches at runtime.

ORDERING NOTE: `pipeline.build.render_site()` clears everything under
`site.output_dir` except archive/ and story/, so it will delete
`indicators/` if it runs afterwards. Run this module (and
`indicators.export`) *after* `python -m pipeline.build`, not before.

Usage: python -m indicators.build
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from indicators.config import get_config, get_countries, resolve_path, validate
from indicators.db import get_connection, init_db
from indicators.export import (
    _active_series,
    _next_release_by_series,
    _observations_by_series,
    _revisions_by_series,
    build_manifest_entry,
    build_series_history,
)
from indicators import presentation
from indicators.presentation import enrich_card

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"

# How many of a series' most recent observations the series page renders as
# a table. The full history is always available from that page's JSON/CSV
# download links (indicators.export), so this is a display cap, not a data
# cap -- a daily series has ~10,000 rows and nobody scrolls that.
SERIES_TABLE_ROWS = 60


def _portable_day_month_year(dt: datetime) -> str:
    """`%-d`/`%#d` (no leading zero) isn't portable across Windows and the
    ubuntu-latest CI runner -- strip the zero manually instead. Deliberately
    duplicated from pipeline/build.py rather than imported: the indicators
    subsystem doesn't depend on pipeline/ modules at runtime.
    """
    return f"{dt.day} {dt.strftime('%b %Y, %H:%M')} UTC"


def _site_meta(site_cfg: dict) -> dict:
    """The `site` dict templates/base.html expects.

    `all_topics`/`all_countries` back base.html's filter row, which is a
    news-article facet UI -- indicator pages pass show_filters=False, so
    both stay empty here rather than being repurposed.
    """
    return {
        "title": site_cfg.get("title", "Economic News Aggregator"),
        "base_url": site_cfg.get("base_url", ""),
        "generated": _portable_day_month_year(datetime.now(timezone.utc)),
        "all_countries": [],
        "all_topics": [],
    }


def _country_label(iso: str, countries: dict) -> str:
    country = countries.get(iso)
    return country["name"] if country else iso.upper()


def _shared_kwargs(asset_prefix: str, canonical_path: str, pagefind_index: bool) -> dict:
    return {
        "asset_prefix": asset_prefix,
        "active_nav": "indicators",
        "canonical_path": canonical_path,
        "pagefind_index": pagefind_index,
        "show_filters": False,
    }


def render_indicators(output_dir: Path) -> dict:
    cfg = get_config()
    site_meta = _site_meta(cfg["site"])
    countries = get_countries()

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # Shared formatting helpers so the series page's history table renders
    # values with the exact same unit/scale conventions as the card's
    # headline value (indicators.presentation), rather than a second,
    # drifting set of format strings living in the template.
    env.filters["fmt_value"] = presentation.value_display
    env.filters["fmt_change"] = presentation.change_display
    env.globals["country_flag"] = presentation.country_flag

    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        series_rows = _active_series(conn)
        observations_by_series = _observations_by_series(conn)
        revisions_by_series = _revisions_by_series(conn)
        next_releases = _next_release_by_series(conn)

    cards = [
        enrich_card(
            build_manifest_entry(
                series,
                observations_by_series.get(series["series_id"], []),
                next_releases.get(series["series_id"]),
                now,
            ),
            now,
            _country_label(series["country"], countries),
        )
        for series in series_rows
    ]
    cards_by_id = {card["series_id"]: card for card in cards}

    indicators_dir = output_dir / "indicators"
    indicators_dir.mkdir(parents=True, exist_ok=True)

    # ---- all-country grid ----
    # pagefind_index=False here and on the country pages: each series has its
    # own canonical page below, and indexing the same numbers three times
    # would triple-hit every search result (same rule pipeline/build.py
    # applies to homepage vs. archive vs. story pages).
    by_country: dict[str, list[dict]] = {}
    for card in cards:
        by_country.setdefault(card["country"], []).append(card)

    country_summaries = [
        {
            "iso": iso,
            "name": _country_label(iso, countries),
            "flag": presentation.country_flag(iso),
            "series_count": len(by_country.get(iso, [])),
            "stale_count": sum(1 for c in by_country.get(iso, []) if c["stale"]),
        }
        for iso in sorted(by_country)
    ]
    topics = sorted({card["topic"] for card in cards})

    index_tmpl = env.get_template("indicators_index.html")
    (indicators_dir / "index.html").write_text(
        index_tmpl.render(
            site=site_meta,
            cards=cards,
            countries=country_summaries,
            topics=topics,
            **_shared_kwargs("../", "indicators/index.html", pagefind_index=False),
        ),
        encoding="utf-8",
    )

    # ---- per-country pages ----
    country_tmpl = env.get_template("indicators_country.html")
    for iso, country_cards in by_country.items():
        country_dir = indicators_dir / iso
        country_dir.mkdir(parents=True, exist_ok=True)
        (country_dir / "index.html").write_text(
            country_tmpl.render(
                site=site_meta,
                country={"iso": iso, "name": _country_label(iso, countries), "flag": presentation.country_flag(iso)},
                cards=country_cards,
                **_shared_kwargs("../../", f"indicators/{iso}/index.html", pagefind_index=False),
            ),
            encoding="utf-8",
        )

    # ---- per-series pages ----
    series_tmpl = env.get_template("indicators_series.html")
    for series in series_rows:
        series_id = series["series_id"]
        iso, indicator_key = series["country"], series["indicator_key"]
        observations = observations_by_series.get(series_id, [])
        history = build_series_history(series, observations, revisions_by_series.get(series_id, []))

        series_dir = indicators_dir / iso / indicator_key
        series_dir.mkdir(parents=True, exist_ok=True)
        (series_dir / "index.html").write_text(
            series_tmpl.render(
                site=site_meta,
                series=history,
                card=cards_by_id[series_id],
                country={"iso": iso, "name": _country_label(iso, countries), "flag": presentation.country_flag(iso)},
                # Newest first for the table -- `observations` itself stays
                # oldest-first everywhere else (chart/CSV order).
                recent_observations=list(reversed(observations))[:SERIES_TABLE_ROWS],
                revisions=list(reversed(history["revisions"])),
                # Relative links to this series' own data files, which
                # indicators.export writes to indicators/data/.
                data_json=f"../../data/{series_id}.json",
                data_csv=f"../../data/{series_id}.csv",
                **_shared_kwargs(
                    "../../../", f"indicators/{iso}/{indicator_key}/index.html", pagefind_index=True
                ),
            ),
            encoding="utf-8",
        )

    return {
        "series": len(series_rows),
        "countries": len(by_country),
        "stale": sum(1 for c in cards if c["stale"]),
        "pages": 1 + len(by_country) + len(series_rows),
    }


def main() -> dict:
    problems = validate()
    if problems:
        # Same hard-fail-on-config-only rule fetch.py follows: a bad config
        # tree means every downstream page would be wrong, so stop rather
        # than publish a partial site.
        print(f"indicators config: {len(problems)} problem(s) -- refusing to build")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)

    init_db()
    cfg = get_config()
    output_dir = resolve_path(cfg["site"].get("output_dir", "site"))

    print("Building indicator pages...")
    stats = render_indicators(output_dir)
    print(
        f"Done: {stats['pages']} page(s) -- {stats['series']} series across "
        f"{stats['countries']} country/countries, {stats['stale']} stale."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
