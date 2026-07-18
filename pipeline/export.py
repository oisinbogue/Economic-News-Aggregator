"""Data exports (brief Phase 5): daily + master CSV, latest.json, RSS feed.

These are the "de facto API" outputs (brief feature #4) plus the raw-data
downloads promised in Sec 2 ("Outputs: HTML site, CSV ... JSON ... RSS").
Everything here is written into the same `site.output_dir` that
`pipeline.build` renders the HTML into, so a single GitHub Pages deploy
publishes all of it together.

This module deliberately reuses `pipeline.build`'s lead-loading/carousel
helpers rather than re-querying the db differently, so "what counts as a
cluster/article for the site" can't drift between the HTML and the
machine-readable exports.

Usage: python -m pipeline.export  (run after `python -m pipeline.build`,
which creates output_dir and copies static assets first)
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from pipeline.build import _attach_carousel, _feed_names, _load_all_leads, load_taxonomy_meta
from pipeline.config import get_config, resolve_path
from pipeline.db import get_connection, init_db

# How many of the most recent clusters go into latest.json/feed.xml -- both
# are meant as a "what's new" feed, not a full corpus dump (that's what the
# CSV exports and the searchable archive are for).
FEED_ITEM_CAP = 50

CSV_FIELDS = [
    "url_hash", "cluster_id", "title", "url", "source", "country",
    "topics", "score", "published", "fetched", "summary",
]


def _csv_row(article: dict, feed_names: dict[int, str]) -> dict:
    return {
        "url_hash": article["url_hash"],
        "cluster_id": article.get("cluster_id"),
        "title": article.get("title") or "",
        "url": article.get("url") or "",
        "source": feed_names.get(article.get("feed_id"), ""),
        "country": article.get("country") or "",
        "topics": article.get("topics") or "",
        "score": article.get("score"),
        "published": article.get("published") or "",
        "fetched": article.get("fetched") or "",
        "summary": article.get("summary") or "",
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_csv_exports(conn, output_dir: Path) -> dict:
    """Writes one CSV per archive day plus a rolling master CSV.

    Scoped to articles that made it into a cluster (`cluster_id IS NOT
    NULL`), matching build.py's search index -- articles still mid-pipeline
    (unclustered, or parked in 'error') aren't part of the published corpus
    yet. Every file is fully regenerated from the db each run rather than
    appended to, so a rerun can't duplicate or drift from what's in SQLite.
    """
    feed_names = _feed_names(conn)
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT url_hash, cluster_id, title, url, feed_id, country, topics,
                   score, published, fetched, summary
            FROM articles
            WHERE cluster_id IS NOT NULL
            ORDER BY fetched DESC
            """
        )
    ]

    exports_dir = output_dir / "exports"
    master_rows = [_csv_row(a, feed_names) for a in rows]
    _write_csv(exports_dir / "master.csv", master_rows)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for a in rows:
        day = (a.get("fetched") or "")[:10] or "unknown"
        by_day[day].append(_csv_row(a, feed_names))
    daily_dir = exports_dir / "daily"
    for day, day_rows in by_day.items():
        _write_csv(daily_dir / f"{day}.csv", day_rows)

    return {"master_rows": len(master_rows), "daily_files": len(by_day)}


def _feed_items(conn, feed_names: dict[int, str], theme_priority: list[str]) -> list[dict]:
    """Most recent clusters, lead article + carousel attached, newest first.

    Shared by latest.json and feed.xml so both "de facto API" surfaces
    (brief feature #4) describe the same set of stories.
    """
    leads = _load_all_leads(conn)[:FEED_ITEM_CAP]
    for lead in leads:
        _attach_carousel(conn, lead, feed_names, theme_priority)
    return leads


def _rationales_for_today(conn) -> dict[int, tuple[int, str]]:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        "SELECT rank, cluster_id, rationale FROM daily_top10 WHERE date = ?", (today,)
    ).fetchall()
    if not rows:
        latest = conn.execute("SELECT MAX(date) AS d FROM daily_top10").fetchone()
        if latest and latest["d"]:
            rows = conn.execute(
                "SELECT rank, cluster_id, rationale FROM daily_top10 WHERE date = ?", (latest["d"],)
            ).fetchall()
    return {row["cluster_id"]: (row["rank"], row["rationale"]) for row in rows}


def _item_json(lead: dict, rationales: dict[int, tuple[int, str]]) -> dict:
    rank_rationale = rationales.get(lead["cluster_id"])
    return {
        "cluster_id": lead["cluster_id"],
        "title": lead["title"],
        "url": lead["url"],
        "source": lead.get("source_name"),
        "summary": lead.get("summary") or "",
        "countries": lead.get("countries") or [],
        "topics": lead.get("topic_list") or [],
        "theme": lead.get("theme"),
        "is_perspectives": lead.get("is_perspectives", False),
        "fetched": lead.get("fetched"),
        "rank": rank_rationale[0] if rank_rationale else None,
        "rationale": rank_rationale[1] if rank_rationale else None,
        "sources_count": len(lead.get("members") or []),
    }


def build_latest_json(conn, site_cfg: dict, leads: list[dict]) -> dict:
    rationales = _rationales_for_today(conn)
    top10 = sorted(
        (l for l in leads if l["cluster_id"] in rationales),
        key=lambda l: rationales[l["cluster_id"]][0],
    )
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "site": {"title": site_cfg.get("title", ""), "base_url": site_cfg.get("base_url", "")},
        "top10": [_item_json(l, rationales) for l in top10],
        "latest": [_item_json(l, rationales) for l in leads],
    }


def _rss_item(lead: dict, base_url: str) -> str:
    link = escape(lead["url"])
    try:
        pub_date = format_datetime(datetime.fromisoformat(lead["fetched"]).replace(tzinfo=timezone.utc))
    except (ValueError, TypeError):
        pub_date = format_datetime(datetime.now(timezone.utc))
    categories = "".join(
        f"<category>{escape(t)}</category>" for t in (lead.get("topic_list") or [])
    )
    guid = escape(f"{base_url}#{lead['cluster_id']}") if base_url else escape(lead["url"])
    return (
        "<item>"
        f"<title>{escape(lead['title'] or '')}</title>"
        f"<link>{link}</link>"
        f"<guid isPermaLink=\"false\">{guid}</guid>"
        f"<pubDate>{pub_date}</pubDate>"
        f"<description>{escape(lead.get('summary') or '')}</description>"
        f"<source>{escape(lead.get('source_name') or '')}</source>"
        f"{categories}"
        "</item>"
    )


def build_rss(leads: list[dict], site_cfg: dict) -> str:
    base_url = site_cfg.get("base_url", "")
    title = escape(site_cfg.get("title", "Economic News Aggregator"))
    self_link = f"{base_url}feed.xml" if base_url else "feed.xml"
    items = "".join(_rss_item(l, base_url) for l in leads)
    build_date = format_datetime(datetime.now(timezone.utc))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "<channel>"
        f"<title>{title}</title>"
        f"<link>{escape(base_url or self_link)}</link>"
        f"<atom:link href=\"{escape(self_link)}\" rel=\"self\" type=\"application/rss+xml\" />"
        "<description>LLM-curated economic news, clustered across sources.</description>"
        "<language>en</language>"
        f"<lastBuildDate>{build_date}</lastBuildDate>"
        f"{items}"
        "</channel>"
        "</rss>"
    )


def write_feed_exports(conn, output_dir: Path) -> dict:
    cfg = get_config()
    site_cfg = cfg["site"]
    taxonomy_meta = load_taxonomy_meta()
    theme_priority = taxonomy_meta["theme_priority"]
    feed_names = _feed_names(conn)

    leads = _feed_items(conn, feed_names, theme_priority)
    latest_json = build_latest_json(conn, site_cfg, leads)

    (output_dir / "latest.json").write_text(
        json.dumps(latest_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "feed.xml").write_text(build_rss(leads, site_cfg), encoding="utf-8")

    return {"feed_items": len(leads), "top10_items": len(latest_json["top10"])}


def main() -> dict:
    init_db()
    cfg = get_config()
    output_dir = resolve_path(cfg["site"].get("output_dir", "site"))
    if not output_dir.exists():
        print("site output_dir doesn't exist yet -- run `python -m pipeline.build` first.")
        sys.exit(1)

    print("Writing exports (CSV, latest.json, feed.xml)...")
    with get_connection() as conn:
        csv_stats = write_csv_exports(conn, output_dir)
        feed_stats = write_feed_exports(conn, output_dir)

    stats = {**csv_stats, **feed_stats}
    print(
        f"Done: master.csv ({stats['master_rows']} rows), {stats['daily_files']} daily CSV(s), "
        f"latest.json ({stats['feed_items']} items, {stats['top10_items']} in top 10), feed.xml."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
