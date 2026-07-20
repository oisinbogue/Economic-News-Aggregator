"""One-off (but safe to re-run) purge of rolling live-blog articles fetched
before pipeline.fetch started skipping them (see LIVE_BLOG_URL_RE and its
2026-07-20 changelog note in pipeline/fetch.py).

A live-blog URL's raw_text can be an unrelated update block from elsewhere
on the same page, so anything derived from it downstream -- tags, cluster
membership, extracted predictions -- is potentially wrong too. This deletes
the article rows themselves plus predictions sourced from them, then tidies
any cluster left with zero members (and any daily_top10 row pointing at it).

Idempotent: finds zero matching rows on every run after the first, so it's
safe to leave wired into the pipeline permanently as a light safety net
rather than removed after one use.

Usage: python -m pipeline.cleanup_live_blogs
"""

from __future__ import annotations

import sys

from pipeline.db import get_connection, init_db
from pipeline.fetch import LIVE_BLOG_URL_RE


def cleanup() -> dict:
    with get_connection() as conn:
        rows = conn.execute("SELECT url_hash, url, cluster_id FROM articles").fetchall()
        purge = [dict(r) for r in rows if LIVE_BLOG_URL_RE.search(r["url"] or "")]
        purge_hashes = [r["url_hash"] for r in purge]
        affected_clusters = {r["cluster_id"] for r in purge if r["cluster_id"] is not None}

        if not purge_hashes:
            return {"articles_deleted": 0, "predictions_deleted": 0, "clusters_deleted": 0}

        placeholders = ",".join("?" * len(purge_hashes))

        predictions_deleted = conn.execute(
            f"DELETE FROM predictions WHERE source IN ({placeholders})", purge_hashes
        ).rowcount

        # Clear representative_article first so deleting the article below
        # never violates clusters.representative_article's FK.
        conn.execute(
            f"UPDATE clusters SET representative_article = NULL "
            f"WHERE representative_article IN ({placeholders})",
            purge_hashes,
        )

        conn.execute(f"DELETE FROM articles WHERE url_hash IN ({placeholders})", purge_hashes)

        clusters_deleted = 0
        for cluster_id in affected_clusters:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM articles WHERE cluster_id = ?", (cluster_id,)
            ).fetchone()["n"]
            if remaining == 0:
                conn.execute("DELETE FROM daily_top10 WHERE cluster_id = ?", (cluster_id,))
                conn.execute("DELETE FROM clusters WHERE id = ?", (cluster_id,))
                clusters_deleted += 1

        return {
            "articles_deleted": len(purge_hashes),
            "predictions_deleted": predictions_deleted,
            "clusters_deleted": clusters_deleted,
        }


def main() -> dict:
    init_db()
    print("Purging previously-fetched live-blog articles (and orphaned clusters/predictions)...")
    stats = cleanup()
    print(
        f"Done: {stats['articles_deleted']} article(s), {stats['predictions_deleted']} prediction(s), "
        f"{stats['clusters_deleted']} now-empty cluster(s) deleted."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
