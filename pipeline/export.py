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

from pipeline.build import _attach_carousels, _feed_names, _load_all_leads, load_taxonomy_meta
from pipeline.config import get_config, resolve_path
from pipeline.timeutil import utc_today
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
    leads = _load_all_leads(conn, limit=FEED_ITEM_CAP)
    _attach_carousels(conn, leads, feed_names, theme_priority)
    return leads


def _rationales_for_today(conn) -> dict[int, tuple[int, str]]:
    today = utc_today().isoformat()
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


def _item_json(lead: dict, rationales: dict[int, tuple[int, str]], base_url: str) -> dict:
    rank_rationale = rationales.get(lead["cluster_id"])
    # `permalink` is the shareable link for this specific cluster
    # (site/story/{cluster_id}.html, see pipeline.build's _render_story_pages)
    # -- `url` stays the original source article's offsite link, unchanged.
    permalink = f"{base_url}{lead['permalink']}" if base_url else lead.get("permalink")
    return {
        "cluster_id": lead["cluster_id"],
        "title": lead["title"],
        "url": lead["url"],
        "permalink": permalink,
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
    base_url = site_cfg.get("base_url", "")
    top10 = sorted(
        (l for l in leads if l["cluster_id"] in rationales),
        key=lambda l: rationales[l["cluster_id"]][0],
    )
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "site": {"title": site_cfg.get("title", ""), "base_url": base_url},
        "top10": [_item_json(l, rationales, base_url) for l in top10],
        "latest": [_item_json(l, rationales, base_url) for l in leads],
    }


def _rss_item(lead: dict, base_url: str) -> str:
    # <link>/<guid> now point at this cluster's own permalink page
    # (site/story/{cluster_id}.html) rather than the original source
    # article -- that page is the one place on our own site that shows the
    # full carousel of takes and the "why it matters" rationale, which is
    # exactly what a reader following an RSS link would want to land on.
    # Falls back to the offsite article URL only when no base_url is
    # configured (dev/local run with no real site to link into).
    permalink = f"{base_url}{lead['permalink']}" if base_url else None
    link = escape(permalink or lead["url"])
    try:
        pub_date = format_datetime(datetime.fromisoformat(lead["fetched"]).replace(tzinfo=timezone.utc))
    except (ValueError, TypeError):
        pub_date = format_datetime(datetime.now(timezone.utc))
    categories = "".join(
        f"<category>{escape(t)}</category>" for t in (lead.get("topic_list") or [])
    )
    guid = escape(permalink) if permalink else escape(lead["url"])
    is_permalink = "true" if permalink else "false"
    description = escape(lead.get("summary") or "")
    if permalink:
        # Original source article stays reachable from the item even though
        # <link> no longer points at it.
        # &#8212; (em dash), not &mdash; -- XML only defines the 5 predefined
        # entities (amp/lt/gt/quot/apos); &mdash; would make this item invalid XML.
        description += f' &#8212; <a href="{escape(lead["url"])}">original source</a>'
    return (
        "<item>"
        f"<title>{escape(lead['title'] or '')}</title>"
        f"<link>{link}</link>"
        f"<guid isPermaLink=\"{is_permalink}\">{guid}</guid>"
        f"<pubDate>{pub_date}</pubDate>"
        f"<description>{description}</description>"
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


def build_sitemap(output_dir: Path, base_url: str) -> str:
    """robots/sitemap discoverability (this module's other outputs -- CSV,
    latest.json, feed.xml -- are the machine-readable "de facto API"; this
    is what actually gets the HTML pages themselves crawled). Walks the
    already-rendered output_dir rather than re-deriving the page list from
    the db, so it can never list a page pipeline.build didn't actually
    write.
    """
    static_pages = ["index.html", "archive/index.html", "health.html", "predictions.html"]
    archive_days = sorted(
        p.relative_to(output_dir).as_posix()
        for p in (output_dir / "archive").glob("*.html")
        if p.name != "index.html"
    )
    story_pages = sorted(
        p.relative_to(output_dir).as_posix()
        for p in (output_dir / "story").glob("*.html")
    ) if (output_dir / "story").exists() else []
    urls = static_pages + archive_days + story_pages

    entries = []
    for url in urls:
        path = output_dir / url
        try:
            lastmod = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        except OSError:
            lastmod = None
        loc = escape(f"{base_url}{'' if url == 'index.html' else url}")
        entry = f"<url><loc>{loc}</loc>"
        if lastmod:
            entry += f"<lastmod>{lastmod}</lastmod>"
        entry += "</url>"
        entries.append(entry)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{''.join(entries)}"
        "</urlset>"
    )


def build_robots(base_url: str) -> str:
    lines = ["User-agent: *", "Allow: /"]
    if base_url:
        lines += ["", f"Sitemap: {base_url}sitemap.xml"]
    return "\n".join(lines) + "\n"


def write_discovery_files(output_dir: Path, site_cfg: dict) -> dict:
    base_url = site_cfg.get("base_url", "")
    sitemap = build_sitemap(output_dir, base_url)
    (output_dir / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    (output_dir / "robots.txt").write_text(build_robots(base_url), encoding="utf-8")
    return {"sitemap_urls": sitemap.count("<url>")}


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

    print("Writing exports (CSV, latest.json, feed.xml, sitemap.xml, robots.txt)...")
    with get_connection() as conn:
        csv_stats = write_csv_exports(conn, output_dir)
        feed_stats = write_feed_exports(conn, output_dir)
    discovery_stats = write_discovery_files(output_dir, cfg["site"])

    stats = {**csv_stats, **feed_stats, **discovery_stats}
    print(
        f"Done: master.csv ({stats['master_rows']} rows), {stats['daily_files']} daily CSV(s), "
        f"latest.json ({stats['feed_items']} items, {stats['top10_items']} in top 10), feed.xml, "
        f"sitemap.xml ({stats['sitemap_urls']} URLs), robots.txt."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
