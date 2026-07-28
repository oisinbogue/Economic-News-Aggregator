"""Static site generator (brief Phase 4).

Renders `data/aggregator.db` into a static site under `site.output_dir`
(config.yaml) with Jinja2 templates: a homepage (LLM-curated top 10 +
category-grouped carousels of everything else clustered in the last
`site.archive_window_days` days) and a full date-indexed archive.

Why client-side search rather than hitting SQLite from the page: the
brief's hosting decision (Sec 2) is GitHub Pages, which serves static
files only -- there's no server to run SQL against. Search (brief
feature #7, "queryable ... from day one") is handled by Pagefind, which
indexes the rendered HTML in a separate `pagefind --site site` step
after this module runs (see the GitHub Actions workflow); the db itself
remains directly queryable for anyone with repo access.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline.cluster import carousel_members_batch
from pipeline.config import get_config, resolve_path
from pipeline.db import get_connection, init_db
from pipeline.timeutil import utc_today

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_DIR = REPO_ROOT / "static"


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


def _group_archive_days(day_summaries: list[dict]) -> list[dict]:
    """Reshape the flat, most-recent-first list of {date, count} into
    year -> month groups so the archive index can render collapsible
    sections instead of one ever-growing flat list (the archive gains a
    day every run, forever). Input order (descending by date) is preserved
    within each month."""
    years: dict[str, dict] = {}
    for d in day_summaries:
        date = d["date"]
        if len(date) >= 7 and date[:4].isdigit():
            year_key, month_key = date[:4], date[:7]
            try:
                month_label = datetime.strptime(month_key, "%Y-%m").strftime("%B %Y")
            except ValueError:
                month_label = month_key
        else:
            year_key, month_key, month_label = "Unknown", "unknown", "Unknown"
        year = years.setdefault(year_key, {"year": year_key, "count": 0, "months": {}})
        year["count"] += d["count"]
        month = year["months"].setdefault(
            month_key, {"key": month_key, "label": month_label, "count": 0, "days": []}
        )
        month["count"] += d["count"]
        month["days"].append(d)

    result = []
    for year_key in sorted(years, reverse=True):
        year = years[year_key]
        year["months"] = [year["months"][k] for k in sorted(year["months"], reverse=True)]
        result.append(year)
    return result


def _feed_names(conn) -> dict[int, str]:
    return {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM feeds")}


def _load_feed_health(conn, recovery_interval_hours: float) -> list[dict]:
    """Per-feed status for the source health dashboard (brief feature #5).

    Reads the live feeds table -- there's no separate history log, just the
    streak/timestamp state fetch.py already maintains, which is enough to
    show what auto-recovery is actually doing: a "healthy" feed working
    fine, a "degraded" feed accumulating failures but still being fetched
    every run, an "inactive" feed that got auto-deactivated and is now
    only probed occasionally (recovery_check_interval_hours) rather than
    abandoned for good the way v1's mark_dead_feeds.py left it, or a "muted"
    feed a human deliberately turned off (config/feeds.yaml active:false --
    see pipeline.reconcile_feeds) which auto-recovery never touches.
    """
    rows = [dict(row) for row in conn.execute("SELECT * FROM feeds ORDER BY name")]
    article_stats = {
        row["feed_id"]: dict(row)
        for row in conn.execute(
            "SELECT feed_id, COUNT(*) AS article_count, "
            "MAX(published) AS last_published, MAX(fetched) AS last_fetched "
            "FROM articles GROUP BY feed_id"
        )
    }
    for row in rows:
        stats = article_stats.get(row["id"], {})
        row["article_count"] = stats.get("article_count", 0)
        # Shown as two separate columns rather than one "last article" figure
        # -- last_published matches what the story cards actually display
        # (see _finalize_lead), while last_fetched is what this dashboard used
        # to report alone. A feed handing back a stale article today should
        # show a recent fetch date next to an old published date, not read as
        # freshly active.
        row["last_published"] = stats.get("last_published")
        row["last_fetched"] = stats.get("last_fetched")
        row["last_published_display"] = _fmt_date(row["last_published"])
        row["last_fetched_display"] = _fmt_date(row["last_fetched"])
        if not row["active"] and row.get("deactivated_reason") == "manual":
            row["status"] = "muted"
            row["next_recovery_check"] = "muted -- not probed"
        elif not row["active"]:
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

    status_order = {"inactive": 0, "degraded": 1, "muted": 2, "healthy": 3}
    rows.sort(key=lambda r: (status_order[r["status"]], -r["consecutive_failures"]))
    return rows


def _load_all_leads(conn, limit: int | None = None) -> list[dict]:
    """One row per cluster: its representative (lead) article, joined with
    feed name for display. Ordered newest-first, optionally capped to the
    `limit` most recent (callers that only need the newest N, e.g. the
    export feed, avoid loading and holding the full cluster history)."""
    query = """
        SELECT clusters.id AS cluster_id, clusters.label,
               articles.title, articles.url, articles.summary,
               articles.country, articles.topics, articles.published,
               articles.fetched, articles.feed_id, feeds.name AS source_name
        FROM clusters
        JOIN articles ON articles.url_hash = clusters.representative_article
        JOIN feeds ON feeds.id = articles.feed_id
        ORDER BY articles.fetched DESC
    """
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    return [dict(row) for row in conn.execute(query, params)]


def _finalize_lead(lead: dict, members: list[dict], feed_names: dict[int, str], theme_priority: list[str]) -> dict:
    for m in members:
        m["source_name"] = feed_names.get(m["feed_id"], "Unknown source")
        m["fetched_display"] = _fmt_date(m.get("published") or m.get("fetched"))

    countries: set[str] = set()
    topics: set[str] = set()
    for m in members:
        countries.update(c for c in (m.get("country") or "").split(",") if c)
        topics.update(t for t in (m.get("topics") or "").split(",") if t)

    lead["members"] = members
    lead["is_perspectives"] = bool(members) and members[0].get("perspectives", False)
    lead["theme"] = dominant_theme(lead.get("topics"), theme_priority)
    lead["countries"] = sorted(countries)
    lead["topic_list"] = sorted(topics)
    lead["fetched_display"] = _fmt_date(lead.get("published") or lead.get("fetched"))
    return lead


def _attach_carousels(
    conn, leads: list[dict], feed_names: dict[int, str], theme_priority: list[str]
) -> list[dict]:
    """Attaches carousel members to every lead in `leads` with a single
    grouped query (pipeline.cluster.carousel_members_batch) instead of one
    carousel_members() query per cluster."""
    members_by_cluster = carousel_members_batch(conn, [lead["cluster_id"] for lead in leads])
    for lead in leads:
        _finalize_lead(lead, members_by_cluster.get(lead["cluster_id"], []), feed_names, theme_priority)
    return leads


def _group_by_theme(leads: list[dict], category_display_order: list[str]) -> list[dict]:
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for lead in leads:
        by_theme[lead["theme"]].append(lead)
    groups = []
    for theme in ordered_themes(set(by_theme), category_display_order):
        groups.append({"name": theme, "clusters": by_theme[theme]})
    return groups


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
    # canonical_id IS NULL excludes rows pipeline.dedup_predictions collapsed
    # into another prediction from the same cluster -- the canonical row
    # (the one duplicates point at) has no canonical_id of its own, so it
    # still shows.
    pending = [
        _prediction_display(dict(row))
        for row in conn.execute(
            "SELECT * FROM predictions WHERE status = 'pending_review' AND canonical_id IS NULL "
            "ORDER BY horizon_date ASC"
        )
    ]

    resolved = [
        dict(row)
        for row in conn.execute(
            """
            SELECT predictions.*, articles.topics AS source_topics, articles.feed_id AS source_feed_id
            FROM predictions
            LEFT JOIN articles ON articles.url_hash = predictions.source
            WHERE predictions.status = 'resolved' AND predictions.canonical_id IS NULL
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
    # archive/ is excluded from this wipe: most archive day pages haven't
    # changed since the last run (see the skip-if-unchanged check below), and
    # deleting them here would force every single one to be rewritten anyway.
    if output_dir.exists():
        for child in output_dir.rglob("*"):
            if child.is_file() and "archive" not in child.relative_to(output_dir).parts[:1]:
                child.unlink(missing_ok=True)
        for child in sorted(output_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if child.is_dir() and "archive" not in child.relative_to(output_dir).parts[:1]:
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
        _attach_carousels(conn, all_leads, feed_names, theme_priority)

        feed_health = _load_feed_health(conn, recovery_interval_hours)
        prediction_data = _load_prediction_data(conn, feed_names)

        today = utc_today().isoformat()
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
        if lead["cluster_id"] not in top10_ids
        # Gate on the same date actually shown on the card (published if the
        # feed supplied one, else fetched) rather than fetched alone -- a
        # feed can hand us a genuinely old article (real published date, not
        # a metadata glitch) on today's fetch, and fetched-only filtering let
        # that display as "recent" on the homepage even though it's from
        # months or years ago. Archive grouping is unaffected: that's meant
        # to be a complete historical record indexed by when we found it.
        and (lead.get("published") or lead.get("fetched") or "") >= window_start
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
        index_tmpl.render(
            site=site_meta, top10=top10, groups=recent_groups, today=today,
            asset_prefix="", active_nav="home", show_filters=True,
            # Every story here also lands permanently on its archive day page,
            # which is the canonical page Pagefind indexes -- keeps search
            # results from showing the same story twice.
            pagefind_index=False,
        ),
        encoding="utf-8",
    )

    # ---- archive: group every cluster by the UTC date of its lead article ----
    by_date: dict[str, list[dict]] = defaultdict(list)
    for lead in all_leads:
        day = (lead.get("fetched") or "")[:10] or "unknown"
        by_date[day].append(lead)

    top10_by_date: dict[str, dict[int, str]] = defaultdict(dict)
    prev_render_state: dict[str, str] = {}
    with get_connection() as conn:
        for row in conn.execute("SELECT date, cluster_id, rationale FROM daily_top10"):
            top10_by_date[row["date"]][row["cluster_id"]] = row["rationale"]
        for row in conn.execute("SELECT date, last_fetched FROM archive_render_state"):
            prev_render_state[row["date"]] = row["last_fetched"]

    archive_days = sorted(by_date, reverse=True)
    archive_index_tmpl = env.get_template("archive_index.html")
    archive_day_tmpl = env.get_template("archive_day.html")

    day_summaries = []
    new_render_state: dict[str, str] = {}
    for day in archive_days:
        leads_for_day = by_date[day]
        day_summaries.append({"date": day, "count": len(leads_for_day)})

        # A day's content is only re-rendered when its most-recently-fetched
        # article has moved on since the last run (or the file is missing) --
        # otherwise this day's archive page is identical to what's already on
        # disk, and rendering it again 6x/day for the site's entire history
        # is pure waste.
        max_fetched = max((lead.get("fetched") or "") for lead in leads_for_day)
        new_render_state[day] = max_fetched
        day_path = output_dir / "archive" / f"{day}.html"
        if prev_render_state.get(day) == max_fetched and day_path.exists():
            continue

        for lead in leads_for_day:
            lead["rationale"] = top10_by_date.get(day, {}).get(lead["cluster_id"])
        groups = _group_by_theme(leads_for_day, category_display_order)
        day_path.write_text(
            archive_day_tmpl.render(
                site=site_meta, day=day, groups=groups, asset_prefix="../", active_nav="archive",
                show_filters=True, pagefind_index=True,
            ),
            encoding="utf-8",
        )

    with get_connection() as conn:
        conn.executemany(
            "INSERT INTO archive_render_state (date, last_fetched) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET last_fetched = excluded.last_fetched",
            list(new_render_state.items()),
        )

    (output_dir / "archive" / "index.html").write_text(
        archive_index_tmpl.render(
            site=site_meta, years=_group_archive_days(day_summaries), asset_prefix="../",
            active_nav="archive", pagefind_index=False,
        ),
        encoding="utf-8",
    )

    # ---- source health dashboard (brief feature #5) ----
    health_tmpl = env.get_template("health.html")
    (output_dir / "health.html").write_text(
        health_tmpl.render(
            site=site_meta, feeds=feed_health,
            recovery_interval_hours=recovery_interval_hours, asset_prefix="", active_nav="health",
            pagefind_index=False,
        ),
        encoding="utf-8",
    )

    # ---- prediction-accuracy tracker (brief feature #8) ----
    predictions_tmpl = env.get_template("predictions.html")
    (output_dir / "predictions.html").write_text(
        predictions_tmpl.render(
            site=site_meta, predictions=prediction_data, asset_prefix="", active_nav="predictions",
            pagefind_index=False,
        ),
        encoding="utf-8",
    )

    # ---- static assets ----
    shutil.copytree(STATIC_DIR, output_dir / "static", dirs_exist_ok=True)

    return {
        "clusters": len(all_leads),
        "top10": len(top10),
        "archive_days": len(archive_days),
        "feeds_inactive": sum(1 for f in feed_health if f["status"] == "inactive"),
        "feeds_degraded": sum(1 for f in feed_health if f["status"] == "degraded"),
        "feeds_muted": sum(1 for f in feed_health if f["status"] == "muted"),
        "predictions_pending_review": len(prediction_data["pending"]),
        "predictions_resolved": prediction_data["resolved_count"],
    }


def main() -> dict:
    init_db()
    print("Building static site...")
    stats = render_site()
    print(
        f"Done: {stats['clusters']} cluster(s) rendered, {stats['top10']} in today's top 10, "
        f"{stats['archive_days']} archive day(s), "
        f"{stats['feeds_inactive']} feed(s) inactive, {stats['feeds_degraded']} degraded, "
        f"{stats['feeds_muted']} muted, "
        f"{stats['predictions_pending_review']} prediction(s) awaiting review, "
        f"{stats['predictions_resolved']} resolved."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
