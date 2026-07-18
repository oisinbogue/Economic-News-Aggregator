"""Static site generator (brief Phase 4).

Renders `data/aggregator.db` into a static site under `site.output_dir`
(config.yaml) with Jinja2 templates: a homepage (LLM-curated top 10 +
category-grouped carousels of everything else clustered in the last
`site.archive_window_days` days), a full date-indexed archive, and a
client-side search/filter index over every article that made it into a
cluster.

Why client-side search rather than hitting SQLite from the page: the
brief's hosting decision (Sec 2) is GitHub Pages, which serves static
files only -- there's no server to run SQL against. `search-index.json`
is the static-hosting-shaped answer to brief feature #7 ("queryable ...
from day one") for the *site*; the db itself remains directly queryable
for anyone with repo access, which is what feature #7 is really about.

Carousel ranking (brief feature #1, "5-article cap ... defined ranking")
is pipeline.cluster.carousel_members -- this module doesn't reimplement
it, just calls it and attaches the display fields (source name, etc.)
the templates need.

Usage: python -m pipeline.build
"""

from __future__ import annotations

import json
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline.cluster import carousel_members
from pipeline.config import get_config, resolve_path
from pipeline.db import get_connection, init_db

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_DIR = REPO_ROOT / "static"

# How much of an article's summary to show in the search index -- keeps
# search-index.json a reasonable download without truncating so hard that
# search results look content-free.
SEARCH_SNIPPET_CHARS = 200


def load_taxonomy_meta() -> dict:
    cfg = get_config()
    path = resolve_path(cfg["paths"]["taxonomy_yaml"])
    with open(path, "r", encoding="utf-8") as f:
        taxonomy = yaml.safe_load(f)
    return {
        "theme_priority": taxonomy.get("theme_priority") or [],
        "category_display_order": taxonomy.get("category_display_order") or [],
    }


def dominant_theme(topics_csv: str | None, theme_priority: list[str]) -> str:
    """Picks the single theme a cluster is grouped under on the site.

    Ported from v1's dominant_theme (aggregator.py:910-921): an article
    matching multiple themes is shown under the highest-priority one (so
    an Irish housing story lands under Ireland, not Global Economy) rather
    than an arbitrary/alphabetical pick.
    """
    topics = [t for t in (topics_csv or "").split(",") if t]
    if not topics:
        return "General"
    for theme in theme_priority:
        if theme in topics:
            return theme
    return sorted(topics)[0]


def ordered_themes(present: set[str], category_display_order: list[str]) -> list[str]:
    ordered = [t for t in category_display_order if t in present]
    ordered += sorted(t for t in present if t not in category_display_order)
    return ordered


def _portable_day_month_year(dt: datetime) -> str:
    """`%-d`/`%#d` (no leading zero) isn't portable across platforms -- this
    runs both on a Windows laptop and ubuntu-latest in CI (see
    .github/workflows/pipeline.yml), so strip the zero manually instead."""
    return f"{dt.day} {dt.strftime('%b %Y, %H:%M')} UTC"


def _fmt_date(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    try:
        return _portable_day_month_year(datetime.fromisoformat(dt_str))
    except ValueError:
        return dt_str


def _feed_names(conn) -> dict[int, str]:
    return {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM feeds")}


def _load_feed_health(conn, recovery_interval_hours: float) -> list[dict]:
    """Per-feed status for the source health dashboard (brief feature #5).

    Reads the live feeds table -- there's no separate history log, just the
    streak/timestamp state fetch.py already maintains, which is enough to
    show what auto-recovery is actually doing: a "healthy" feed working
    fine, a "degraded" feed accumulating failures but still being fetched
    every run, or an "inactive" feed that got auto-deactivated and is now
    only probed occasionally (recovery_check_interval_hours) rather than
    abandoned for good the way v1's mark_dead_feeds.py left it.
    """
    rows = [dict(row) for row in conn.execute("SELECT * FROM feeds ORDER BY name")]
    for row in rows:
        if not row["active"]:
            row["status"] = "inactive"
            if row.get("last_attempt"):
                next_check = datetime.fromisoformat(row["last_attempt"]) + timedelta(hours=recovery_interval_hours)
                row["next_recovery_check"] = _fmt_date(next_check.isoformat())
            else:
                row["next_recovery_check"] = "next run"
        elif row["consecutive_failures"] > 0:
            row["status"] = "degraded"
        else:
            row["status"] = "healthy"
        row["last_success_display"] = _fmt_date(row.get("last_success"))
        row["last_attempt_display"] = _fmt_date(row.get("last_attempt"))

    status_order = {"inactive": 0, "degraded": 1, "healthy": 2}
    rows.sort(key=lambda r: (status_order[r["status"]], -r["consecutive_failures"]))
    return rows


def _load_all_leads(conn) -> list[dict]:
    """One row per cluster: its representative (lead) article, joined with
    feed name for display. Ordered newest-first."""
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT clusters.id AS cluster_id, clusters.label,
                   articles.title, articles.url, articles.summary,
                   articles.country, articles.topics, articles.published,
                   articles.fetched, articles.feed_id, feeds.name AS source_name
            FROM clusters
            JOIN articles ON articles.url_hash = clusters.representative_article
            JOIN feeds ON feeds.id = articles.feed_id
            ORDER BY articles.fetched DESC
            """
        )
    ]


def _attach_carousel(conn, lead: dict, feed_names: dict[int, str], theme_priority: list[str]) -> dict:
    members = carousel_members(conn, lead["cluster_id"])
    for m in members:
        m["source_name"] = feed_names.get(m["feed_id"], "Unknown source")
        m["fetched_display"] = _fmt_date(m.get("fetched"))

    countries = sorted({m["country"] for m in members if m.get("country")})
    topics: set[str] = set()
    for m in members:
        topics.update(t for t in (m.get("topics") or "").split(",") if t)

    lead["members"] = members
    lead["is_perspectives"] = bool(members) and members[0].get("perspectives", False)
    lead["theme"] = dominant_theme(lead.get("topics"), theme_priority)
    lead["countries"] = countries
    lead["topic_list"] = sorted(topics)
    lead["fetched_display"] = _fmt_date(lead.get("fetched"))
    return lead


def _group_by_theme(leads: list[dict], category_display_order: list[str]) -> list[dict]:
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for lead in leads:
        by_theme[lead["theme"]].append(lead)
    groups = []
    for theme in ordered_themes(set(by_theme), category_display_order):
        groups.append({"name": theme, "clusters": by_theme[theme]})
    return groups


def build_search_index(conn, feed_names: dict[int, str]) -> list[dict]:
    rows = conn.execute(
        """
        SELECT articles.title, articles.url, articles.summary, articles.country,
               articles.topics, articles.fetched, articles.feed_id, articles.cluster_id
        FROM articles
        WHERE articles.cluster_id IS NOT NULL
        ORDER BY articles.fetched DESC
        """
    )
    index = []
    for row in rows:
        d = dict(row)
        summary = (d.get("summary") or "")[:SEARCH_SNIPPET_CHARS]
        index.append({
            "t": d["title"],
            "u": d["url"],
            "s": feed_names.get(d["feed_id"], ""),
            "c": d.get("country") or "",
            "tp": [t for t in (d.get("topics") or "").split(",") if t],
            "d": (d.get("fetched") or "")[:10],
            "sm": summary,
            "cl": d["cluster_id"],
        })
    return index


def _prediction_display(p: dict) -> dict:
    evidence = json.loads(p["verdict_evidence"]) if p.get("verdict_evidence") else {"reasoning": "", "articles": []}
    p["reasoning"] = evidence.get("reasoning", "")
    p["evidence_articles"] = evidence.get("articles", [])
    return p


def _load_prediction_data(conn, feed_names: dict[int, str]) -> dict:
    """Resolved-prediction leaderboard + read-only pending-review queue for
    predictions.html (brief feature #8, "Output"). Confirmation itself
    happens locally via `python -m pipeline.review` -- see that module's
    docstring for why a static GitHub Pages site can't do it in-browser.
    """
    pending = [
        _prediction_display(dict(row))
        for row in conn.execute(
            "SELECT * FROM predictions WHERE status = 'pending_review' ORDER BY horizon_date ASC"
        )
    ]

    resolved = [
        dict(row)
        for row in conn.execute(
            """
            SELECT predictions.*, articles.topics AS source_topics, articles.feed_id AS source_feed_id
            FROM predictions
            LEFT JOIN articles ON articles.url_hash = predictions.source
            WHERE predictions.status = 'resolved'
            """
        )
    ]

    def _accuracy(rows: list[dict]) -> dict:
        correct = sum(1 for r in rows if r["verdict"] == "correct")
        incorrect = sum(1 for r in rows if r["verdict"] == "incorrect")
        unresolvable = sum(1 for r in rows if r["verdict"] == "unresolvable")
        denom = correct + incorrect
        return {
            "total": len(rows), "correct": correct, "incorrect": incorrect,
            "unresolvable": unresolvable,
            "accuracy_pct": round(100 * correct / denom) if denom else None,
        }

    by_predictor: dict[str, list[dict]] = defaultdict(list)
    for r in resolved:
        by_predictor[r["predictor"] or "Unknown"].append(r)
    predictor_leaderboard = [
        {"name": name, **_accuracy(rows)} for name, rows in by_predictor.items()
    ]
    predictor_leaderboard.sort(key=lambda r: (-(r["accuracy_pct"] or -1), -r["total"]))

    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in resolved:
        if r.get("source_feed_id"):
            by_source[feed_names.get(r["source_feed_id"], "Unknown source")].append(r)
    source_leaderboard = [
        {"name": name, **_accuracy(rows)} for name, rows in by_source.items()
    ]
    source_leaderboard.sort(key=lambda r: (-(r["accuracy_pct"] or -1), -r["total"]))

    by_topic: dict[str, list[dict]] = defaultdict(list)
    for r in resolved:
        topics = [t for t in (r.get("source_topics") or "").split(",") if t] or ["Untagged"]
        for t in topics:
            by_topic[t].append(r)
    topic_leaderboard = [
        {"name": name, **_accuracy(rows)} for name, rows in by_topic.items()
    ]
    topic_leaderboard.sort(key=lambda r: (-(r["accuracy_pct"] or -1), -r["total"]))

    return {
        "pending": pending,
        "predictor_leaderboard": predictor_leaderboard,
        "source_leaderboard": source_leaderboard,
        "topic_leaderboard": topic_leaderboard,
        "resolved_count": len(resolved),
    }


def render_site() -> dict:
    cfg = get_config()
    site_cfg = cfg["site"]
    taxonomy_meta = load_taxonomy_meta()
    theme_priority = taxonomy_meta["theme_priority"]
    category_display_order = taxonomy_meta["category_display_order"]
    window_days = site_cfg.get("archive_window_days", 3)

    output_dir = resolve_path(site_cfg.get("output_dir", "site"))
    # rmtree-then-recreate is simpler but OneDrive (this repo's own folder,
    # per launch context) can hold a transient lock on a synced directory
    # right after another process touches it, turning that into a flaky
    # PermissionError. Clearing file-by-file and tolerating undeletable
    # directories (they get reused, not left stale-but-wrong) avoids that.
    if output_dir.exists():
        for child in output_dir.rglob("*"):
            if child.is_file():
                child.unlink(missing_ok=True)
        for child in sorted(output_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "archive").mkdir(exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    recovery_interval_hours = cfg["run"].get("recovery_check_interval_hours", 24)

    with get_connection() as conn:
        feed_names = _feed_names(conn)
        all_leads = _load_all_leads(conn)
        for lead in all_leads:
            _attach_carousel(conn, lead, feed_names, theme_priority)

        feed_health = _load_feed_health(conn, recovery_interval_hours)
        prediction_data = _load_prediction_data(conn, feed_names)

        today = date.today().isoformat()
        top10_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT rank, cluster_id, rationale FROM daily_top10 WHERE date = ? ORDER BY rank",
                (today,),
            )
        ]
        if not top10_rows:
            # No curate run yet today (e.g. first run of the day hasn't
            # happened) -- fall back to the most recent date we have so the
            # homepage isn't just empty.
            latest = conn.execute("SELECT MAX(date) AS d FROM daily_top10").fetchone()
            if latest and latest["d"]:
                today = latest["d"]
                top10_rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT rank, cluster_id, rationale FROM daily_top10 WHERE date = ? ORDER BY rank",
                        (today,),
                    )
                ]

        search_index = build_search_index(conn, feed_names)

    leads_by_id = {lead["cluster_id"]: lead for lead in all_leads}

    top10 = []
    for row in top10_rows:
        lead = leads_by_id.get(row["cluster_id"])
        if not lead:
            continue
        top10.append({**lead, "rank": row["rank"], "rationale": row["rationale"]})
    top10_ids = {t["cluster_id"] for t in top10}

    window_start = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    recent_leads = [
        lead for lead in all_leads
        if lead["cluster_id"] not in top10_ids and (lead.get("fetched") or "") >= window_start
    ]
    recent_groups = _group_by_theme(recent_leads, category_display_order)

    all_countries = sorted({c for lead in all_leads for c in lead["countries"]})
    all_topics = sorted({t for lead in all_leads for t in lead["topic_list"]})

    site_meta = {
        "title": site_cfg.get("title", "Economic News Aggregator"),
        "base_url": site_cfg.get("base_url", ""),
        "generated": _portable_day_month_year(datetime.now(timezone.utc)),
        "all_countries": all_countries,
        "all_topics": all_topics,
    }

    # ---- homepage ----
    index_tmpl = env.get_template("index.html")
    (output_dir / "index.html").write_text(
        index_tmpl.render(site=site_meta, top10=top10, groups=recent_groups, today=today, asset_prefix=""),
        encoding="utf-8",
    )

    # ---- archive: group every cluster by the UTC date of its lead article ----
    by_date: dict[str, list[dict]] = defaultdict(list)
    for lead in all_leads:
        day = (lead.get("fetched") or "")[:10] or "unknown"
        by_date[day].append(lead)

    top10_by_date: dict[str, dict[int, str]] = defaultdict(dict)
    with get_connection() as conn:
        for row in conn.execute("SELECT date, cluster_id, rationale FROM daily_top10"):
            top10_by_date[row["date"]][row["cluster_id"]] = row["rationale"]

    archive_days = sorted(by_date, reverse=True)
    archive_index_tmpl = env.get_template("archive_index.html")
    archive_day_tmpl = env.get_template("archive_day.html")

    day_summaries = []
    for day in archive_days:
        leads_for_day = by_date[day]
        day_summaries.append({"date": day, "count": len(leads_for_day)})

        for lead in leads_for_day:
            lead["rationale"] = top10_by_date.get(day, {}).get(lead["cluster_id"])
        groups = _group_by_theme(leads_for_day, category_display_order)
        (output_dir / "archive" / f"{day}.html").write_text(
            archive_day_tmpl.render(site=site_meta, day=day, groups=groups, asset_prefix="../"),
            encoding="utf-8",
        )

    (output_dir / "archive" / "index.html").write_text(
        archive_index_tmpl.render(site=site_meta, days=day_summaries, asset_prefix="../"),
        encoding="utf-8",
    )

    # ---- source health dashboard (brief feature #5) ----
    health_tmpl = env.get_template("health.html")
    (output_dir / "health.html").write_text(
        health_tmpl.render(
            site=site_meta, feeds=feed_health,
            recovery_interval_hours=recovery_interval_hours, asset_prefix="",
        ),
        encoding="utf-8",
    )

    # ---- prediction-accuracy tracker (brief feature #8) ----
    predictions_tmpl = env.get_template("predictions.html")
    (output_dir / "predictions.html").write_text(
        predictions_tmpl.render(site=site_meta, predictions=prediction_data, asset_prefix=""),
        encoding="utf-8",
    )

    # ---- search index + static assets ----
    (output_dir / "search-index.json").write_text(
        json.dumps(search_index, ensure_ascii=False), encoding="utf-8"
    )
    shutil.copytree(STATIC_DIR, output_dir / "static", dirs_exist_ok=True)

    return {
        "clusters": len(all_leads),
        "top10": len(top10),
        "archive_days": len(archive_days),
        "search_index_articles": len(search_index),
        "feeds_inactive": sum(1 for f in feed_health if f["status"] == "inactive"),
        "feeds_degraded": sum(1 for f in feed_health if f["status"] == "degraded"),
        "predictions_pending_review": len(prediction_data["pending"]),
        "predictions_resolved": prediction_data["resolved_count"],
    }


def main() -> dict:
    init_db()
    print("Building static site...")
    stats = render_site()
    print(
        f"Done: {stats['clusters']} cluster(s) rendered, {stats['top10']} in today's top 10, "
        f"{stats['archive_days']} archive day(s), {stats['search_index_articles']} article(s) in the search index, "
        f"{stats['feeds_inactive']} feed(s) inactive, {stats['feeds_degraded']} degraded, "
        f"{stats['predictions_pending_review']} prediction(s) awaiting review, "
        f"{stats['predictions_resolved']} resolved."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
