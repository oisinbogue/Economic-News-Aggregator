"""Story clustering, adapted for the carousel UI (brief feature #1).

Ported from aggregator.py:568-676 (cluster_articles, _sig_words): two
articles are linked when their titles share >= `cluster.title_shared_words`
significant words, OR their summaries have TF-IDF cosine similarity above
`cluster.tfidf_cosine_threshold` (requires scikit-learn; degrades gracefully
to title-word matching only if it isn't installed, same as v1).

Differences from v1's one-shot batch clustering, needed because this
pipeline runs incrementally through the day rather than once:
  - Only articles fetched within `cluster.window_days` are eligible to join
    or form a cluster, so union-find can't link unrelated stories that
    happen to reuse the same recurring headline words months apart.
  - A new article is first checked against *existing* recent clusters (so a
    story that breaks in one run and gets follow-up coverage in a later run
    the same day still lands in the same cluster) before forming new
    clusters via union-find among the remaining unmatched candidates.
  - v1 built one eagerly-truncated "also" list per cluster at clustering
    time. Here every cluster member stays in the db (the archive is meant
    to be a complete, searchable corpus -- brief feature #7) and the 5-cap
    for the carousel is applied at read time by `carousel_members()`, which
    Phase 4's site templates will call.

Usage: python -m pipeline.cluster
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone

from pipeline.config import get_config
from pipeline.db import get_connection, init_db

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as cos_sim
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

# Ported from aggregator.py:138-145.
_STOPWORDS = {
    'a', 'an', 'the', 'in', 'on', 'at', 'of', 'to', 'for', 'is', 'are', 'was', 'were',
    'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'shall', 'can', 'and', 'or', 'but', 'not',
    'with', 'from', 'by', 'as', 'its', 'it', 'this', 'that', 'their', 'they',
    'he', 'she', 'we', 'i', 'you', 'up', 'out', 'than', 'more', 'over', 'new',
    'about', 'after', 'into', 'how', 's', 'us',
}


def _sig_words(title: str) -> set[str]:
    """Significant words in a title: lowercase, no punctuation, no stopwords, len > 2."""
    words = re.sub(r"[^\w\s]", " ", (title or "").lower()).split()
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _cluster_text(row: dict) -> str:
    return row.get("summary") or row.get("title") or ""


def process_all() -> dict:
    cfg = get_config()["cluster"]
    window_days = cfg.get("window_days", 3)
    min_shared_words = cfg.get("title_shared_words", 3)
    cosine_threshold = cfg.get("tfidf_cosine_threshold", 0.65)

    window_start = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

    with get_connection() as conn:
        leads = [
            dict(row)
            for row in conn.execute(
                """
                SELECT clusters.id AS cluster_id, articles.title, articles.summary
                FROM clusters
                JOIN articles ON articles.url_hash = clusters.representative_article
                WHERE articles.fetched >= ?
                """,
                (window_start,),
            )
        ]
        candidates = [
            dict(row)
            for row in conn.execute(
                """
                SELECT url_hash, title, summary, score
                FROM articles
                WHERE cluster_id IS NULL AND topics IS NOT NULL AND fetched >= ?
                ORDER BY fetched
                """,
                (window_start,),
            )
        ]

    stats = {"joined_existing": 0, "new_clusters": 0, "new_cluster_members": 0}
    if not candidates:
        return stats

    texts = [_cluster_text(r) for r in leads] + [_cluster_text(r) for r in candidates]
    sig = [_sig_words(r["title"]) for r in leads] + [_sig_words(r["title"]) for r in candidates]

    sim_matrix = None
    if SKLEARN_AVAILABLE and len(texts) >= 2:
        try:
            vec = TfidfVectorizer(stop_words="english", max_features=5000)
            tfidf = vec.fit_transform(texts)
            sim_matrix = cos_sim(tfidf)
        except ValueError:
            pass  # e.g. all-stopword corpus -- fall back to title-word matching only

    def is_match(i: int, j: int) -> bool:
        title_match = len(sig[i] & sig[j]) >= min_shared_words
        cos_match = sim_matrix is not None and float(sim_matrix[i, j]) > cosine_threshold
        return title_match or cos_match

    n_leads = len(leads)
    n_cand = len(candidates)

    # Pass 1: attach candidates to an existing recent cluster where possible.
    attached = [False] * n_cand
    with get_connection() as conn:
        for ci in range(n_cand):
            for li in range(n_leads):
                if is_match(li, n_leads + ci):
                    conn.execute(
                        "UPDATE articles SET cluster_id = ? WHERE url_hash = ?",
                        (leads[li]["cluster_id"], candidates[ci]["url_hash"]),
                    )
                    attached[ci] = True
                    stats["joined_existing"] += 1
                    break

    # Pass 2: union-find brand-new clusters among the still-unattached candidates.
    remaining = [ci for ci in range(n_cand) if not attached[ci]]
    parent = {ci: ci for ci in remaining}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for a_idx, ci in enumerate(remaining):
        for cj in remaining[a_idx + 1:]:
            if is_match(n_leads + ci, n_leads + cj):
                union(ci, cj)

    groups: dict[int, list[int]] = {}
    for ci in remaining:
        groups.setdefault(find(ci), []).append(ci)

    with get_connection() as conn:
        created_at = datetime.now(timezone.utc).isoformat()
        for members in groups.values():
            # Highest-scored member (score may be NULL pre-tagging edge case -> 0) leads.
            members.sort(key=lambda ci: -(candidates[ci]["score"] or 0))
            lead_hash = candidates[members[0]]["url_hash"]
            label = candidates[members[0]]["title"]
            if label and len(label) > 80:
                label = label[:80] + "..."

            cur = conn.execute(
                "INSERT INTO clusters (created, label, representative_article) VALUES (?, ?, ?)",
                (created_at, label, lead_hash),
            )
            cluster_id = cur.lastrowid
            for ci in members:
                conn.execute(
                    "UPDATE articles SET cluster_id = ? WHERE url_hash = ?",
                    (cluster_id, candidates[ci]["url_hash"]),
                )
            stats["new_clusters"] += 1
            stats["new_cluster_members"] += len(members)

    return stats


def carousel_members(conn, cluster_id: int, cap: int | None = None) -> list[dict]:
    """Ranking rule for "up to 5 articles, different outlets' takes" (brief
    feature #1): highest-scored article per source feed (so near-duplicate
    wire copy from the same outlet doesn't crowd out other perspectives),
    ranked by score desc then earliest-fetched first, capped at `cap`.
    Also flags `perspectives=True` when the cluster spans >= 3 countries,
    mirroring v1's cross-region flag (aggregator.py:626-628) with `country`
    in place of the old `region_slug`.
    """
    if cap is None:
        cap = get_config()["cluster"].get("carousel_cap", 5)

    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT url_hash, feed_id, title, url, summary, country, topics, score, fetched, image
            FROM articles
            WHERE cluster_id = ?
            ORDER BY (score IS NULL), score DESC, fetched ASC
            """,
            (cluster_id,),
        )
    ]

    seen_feeds: set = set()
    diverse: list[dict] = []
    for row in rows:
        if row["feed_id"] in seen_feeds:
            continue
        seen_feeds.add(row["feed_id"])
        diverse.append(row)

    distinct_countries = {r["country"] for r in rows if r["country"]}
    for row in diverse:
        row["perspectives"] = len(distinct_countries) >= 3

    return diverse[:cap]


def main() -> dict:
    init_db()
    print("Clustering tagged articles...")
    stats = process_all()
    print(
        f"Done: {stats['joined_existing']} joined an existing cluster, "
        f"{stats['new_clusters']} new cluster(s) formed from "
        f"{stats['new_cluster_members']} article(s)."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
