"""Syncs the `feeds` table to config/feeds.yaml -- the single source of
truth for which feeds the pipeline tracks. Runs at the start of every
pipeline invocation (before fetch), not just once ever.

This is also the documented way to permanently mute a noisy/low-quality feed
without it silently coming back: set `active: false` on that feed's entry in
config/feeds.yaml and rerun this script (or just let the next scheduled
pipeline run do it). That's recorded as deactivated_reason='manual', which
pipeline.fetch's auto-recovery probe explicitly skips -- unlike an
auto-deactivated feed (MAX_CONSECUTIVE_FAILURES), a manually muted one is
never retried, so a lucky probe can't flip it back on behind your back. To
unmute it, flip the YAML back to `active: true` and rerun.

For every feed listed under `regions:` in the YAML:
  - new url                    -> inserted, active = the YAML's `active` flag,
                                   deactivated_reason = 'manual' if inactive.
  - existing url, YAML active: false -> name/country/language/topic_hint
                                         updated, active forced to 0 and
                                         deactivated_reason forced to
                                         'manual'. An explicit "this is dead"
                                         in the YAML always wins.
  - existing url, YAML active: true  -> name/country/language/topic_hint
                                         updated. If the feed was manually
                                         muted (deactivated_reason='manual'),
                                         the human has now said otherwise in
                                         the YAML, so it's reactivated
                                         immediately (active=1,
                                         deactivated_reason=NULL) rather than
                                         waiting on a recovery probe. Otherwise
                                         active/deactivated_reason are left
                                         alone -- whether a previously-healthy
                                         feed is currently active is
                                         pipeline.fetch's call
                                         (MAX_CONSECUTIVE_FAILURES / recovery
                                         probes), not this script's, and
                                         forcing active=1 here every run would
                                         fight that cooldown and defeat the
                                         auto-recovery backoff.
  - db url absent from the YAML      -> deactivated (active=0,
                                         deactivated_reason='manual'), never
                                         deleted, so a source pulled from the
                                         YAML doesn't lose its fetch history.

Feed health columns (consecutive_failures, last_success, etag,
last_modified, last_error_type, last_attempt) are never touched here --
only pipeline.fetch owns those.

Two YAML rows can resolve to the same `url` (e.g. a discovered feed that
turned out to be the same URL as another source) -- last one processed wins,
same dedup-by-url behaviour the feeds table's UNIQUE constraint already
enforced under the old feeds.csv import path.

Usage: python -m pipeline.reconcile_feeds  (runs automatically at the start
of every scheduled pipeline run, before pipeline.fetch)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import yaml

from pipeline.config import get_config, resolve_path
from pipeline.db import get_connection, init_db


@dataclass
class ReconcileStats:
    inserted: int = 0
    updated: int = 0
    deactivated: int = 0


def load_feeds_yaml(path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    feeds = []
    for region_feeds in (data.get("regions") or {}).values():
        for row in region_feeds or []:
            feeds.append(row)
    return feeds


def reconcile(feeds: list[dict]) -> ReconcileStats:
    stats = ReconcileStats()
    seen_urls: set[str] = set()

    with get_connection() as conn:
        existing = {
            row["url"]: dict(row) for row in conn.execute("SELECT * FROM feeds")
        }

        for row in feeds:
            url = (row.get("url") or "").strip()
            if not url:
                continue
            seen_urls.add(url)

            name = row.get("name", "")
            country = row.get("country", "")
            language = row.get("language", "")
            topic_hint = row.get("topic_hint", "")
            yaml_active = 1 if row.get("active", True) else 0

            if url not in existing:
                conn.execute(
                    """
                    INSERT INTO feeds (url, name, country, language, topic_hint, active, deactivated_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (url, name, country, language, topic_hint, yaml_active, None if yaml_active else "manual"),
                )
                existing[url] = {"active": yaml_active, "deactivated_reason": None if yaml_active else "manual"}
                stats.inserted += 1
            else:
                if yaml_active:
                    was_manually_muted = existing[url].get("deactivated_reason") == "manual"
                    if was_manually_muted:
                        # The human muted this feed via active:false and has
                        # now flipped it back -- reactivate right away rather
                        # than waiting on a recovery probe.
                        conn.execute(
                            """
                            UPDATE feeds
                            SET name = ?, country = ?, language = ?, topic_hint = ?,
                                active = 1, deactivated_reason = NULL
                            WHERE url = ?
                            """,
                            (name, country, language, topic_hint, url),
                        )
                    else:
                        # Leave active/deactivated_reason alone -- owned by
                        # pipeline.fetch's health tracking once a feed exists
                        # in the db.
                        conn.execute(
                            """
                            UPDATE feeds
                            SET name = ?, country = ?, language = ?, topic_hint = ?
                            WHERE url = ?
                            """,
                            (name, country, language, topic_hint, url),
                        )
                else:
                    conn.execute(
                        """
                        UPDATE feeds
                        SET name = ?, country = ?, language = ?, topic_hint = ?,
                            active = 0, deactivated_reason = 'manual'
                        WHERE url = ?
                        """,
                        (name, country, language, topic_hint, url),
                    )
                stats.updated += 1

        removed_urls = set(existing) - seen_urls
        for url in removed_urls:
            if existing[url]["active"]:
                conn.execute(
                    "UPDATE feeds SET active = 0, deactivated_reason = 'manual' WHERE url = ?",
                    (url,),
                )
                stats.deactivated += 1

    return stats


def main() -> None:
    init_db()
    cfg = get_config()
    feeds_yaml = resolve_path(cfg["paths"]["feeds_yaml"])

    if not feeds_yaml.exists():
        print(f"feeds.yaml not found at {feeds_yaml}", file=sys.stderr)
        sys.exit(1)

    feeds = load_feeds_yaml(feeds_yaml)
    if not feeds:
        print("feeds.yaml has no feeds to reconcile.")
        return

    stats = reconcile(feeds)
    print(
        f"Reconciled {len(feeds)} feed(s) from feeds.yaml: "
        f"{stats.inserted} inserted, {stats.updated} updated, "
        f"{stats.deactivated} deactivated (removed from YAML)."
    )


if __name__ == "__main__":
    main()
