"""Story clustering, adapted for the carousel UI (brief feature #1).

Two articles are linked when the cosine similarity of their summary
embeddings (pipeline.embed, sentence-transformers/all-MiniLM-L6-v2 via
fastembed) exceeds `cluster.embedding_cosine_threshold`. Embeddings handle
paraphrased headlines and translated sources (which by this point in the
pipeline have an English summary -- see pipeline.summarize) far better than
the original title-word/TF-IDF approach, which is why it replaced TF-IDF
outright rather than running alongside it. Title-word overlap
(`cluster.title_shared_words`) is kept only as a degraded fallback for the
rare case where an article reaches this stage without an embedding yet
(e.g. pipeline.embed hasn't run this cycle) -- same spirit as the old
sklearn-unavailable fallback it replaces.

Threshold-selection notes (2026-07-20): this DB had zero real multi-article
clusters to sample precision/recall from -- 28 clusters, 2408 articles, and
every existing cluster had exactly one member, because the TF-IDF/title
matcher it's replacing had essentially never fired. There was nothing to
grade a threshold against. Instead: embedded the 28 real summarised
articles in the DB and looked at all pairwise cosine similarities -- the
highest similarity between any two genuinely DIFFERENT stories was 0.583
(two UK-economy-adjacent but distinct articles). Then hand-wrote 5
paraphrases of real summaries (same facts, different wording, simulating a
second outlet's or a translated take on the same story) and confirmed each
correctly best-matched its real source at 0.70-0.83 similarity, well clear
of every real negative pair. 0.62 sits in that gap with margin on both
sides. This is a small sample (real duplicate-story pairs may look
different from synthetic paraphrases), so treat 0.62 as a reasonable
starting point to revisit once genuine multi-outlet coverage of the same
story shows up in the archive, not a final answer.

Cross-window continuity ("developing story" chains, brief feature #2): after
same-day clustering, each newly-formed cluster's centroid (mean member
embedding) is compared against the centroids of clusters created between
`cluster.window_days` and `cluster.chain_window_days` ago. The best match
above `cluster.chain_cosine_threshold` (deliberately stricter than same-day
clustering -- a false chain is worse than a missed one) is recorded as
`clusters.parent_cluster_id`, a link rather than a merge, so each day's
cluster stays a distinct carousel entry while the thread stays traceable by
walking parent_cluster_id back through time.

Usage: python -m pipeline.cluster
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from pipeline.config import get_config
from pipeline.db import get_connection, init_db
from pipeline.embed import unpack_vector

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


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _fetch_embeddings(conn, url_hashes: list[str], model: str) -> dict[str, np.ndarray]:
    if not url_hashes:
        return {}
    placeholders = ",".join("?" * len(url_hashes))
    return {
        row["url_hash"]: unpack_vector(row["vector"])
        for row in conn.execute(
            f"SELECT url_hash, vector FROM article_embeddings "
            f"WHERE model = ? AND url_hash IN ({placeholders})",
            (model, *url_hashes),
        )
    }


def process_all() -> dict:
    cfg = get_config()["cluster"]
    embed_model = get_config()["embed"]["model"]
    window_days = cfg.get("window_days", 3)
    min_shared_words = cfg.get("title_shared_words", 3)
    embedding_threshold = cfg.get("embedding_cosine_threshold", 0.62)
    chain_window_days = cfg.get("chain_window_days", 14)
    chain_threshold = cfg.get("chain_cosine_threshold", 0.78)

    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=window_days)).isoformat()

    with get_connection() as conn:
        leads = [
            dict(row)
            for row in conn.execute(
                """
                SELECT clusters.id AS cluster_id, articles.url_hash, articles.title
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
                SELECT url_hash, title, score
                FROM articles
                WHERE cluster_id IS NULL AND topics IS NOT NULL AND fetched >= ?
                ORDER BY fetched
                """,
                (window_start,),
            )
        ]

    stats = {"joined_existing": 0, "new_clusters": 0, "new_cluster_members": 0, "chained": 0}
    if not candidates:
        return stats

    hashes = [r["url_hash"] for r in leads] + [r["url_hash"] for r in candidates]
    sig = [_sig_words(r["title"]) for r in leads] + [_sig_words(r["title"]) for r in candidates]

    with get_connection() as conn:
        embeddings = _fetch_embeddings(conn, hashes, embed_model)

    def is_match(i: int, j: int) -> bool:
        vi, vj = embeddings.get(hashes[i]), embeddings.get(hashes[j])
        if vi is not None and vj is not None:
            return _cosine(vi, vj) > embedding_threshold
        # Degraded fallback: one or both articles have no embedding yet
        # (pipeline.embed hasn't caught up this cycle) -- title-word overlap
        # only, same as the old sklearn-unavailable fallback it replaces.
        return len(sig[i] & sig[j]) >= min_shared_words

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

    new_cluster_vectors: dict[int, list[np.ndarray]] = {}
    with get_connection() as conn:
        created_at = now.isoformat()
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
            member_vectors = []
            for ci in members:
                conn.execute(
                    "UPDATE articles SET cluster_id = ? WHERE url_hash = ?",
                    (cluster_id, candidates[ci]["url_hash"]),
                )
                v = embeddings.get(candidates[ci]["url_hash"])
                if v is not None:
                    member_vectors.append(v)
            new_cluster_vectors[cluster_id] = member_vectors
            stats["new_clusters"] += 1
            stats["new_cluster_members"] += len(members)

        # Cross-window continuity: link each new cluster to the closest
        # older cluster (window_days..chain_window_days ago), if any clears
        # the stricter chain threshold. A link, not a merge -- see module
        # docstring.
        chain_start = (now - timedelta(days=chain_window_days)).isoformat()
        prior_vectors: dict[int, list[np.ndarray]] = defaultdict(list)
        for row in conn.execute(
            """
            SELECT clusters.id AS cluster_id, article_embeddings.vector
            FROM clusters
            JOIN articles ON articles.cluster_id = clusters.id
            JOIN article_embeddings
                ON article_embeddings.url_hash = articles.url_hash
                AND article_embeddings.model = ?
            WHERE clusters.created >= ? AND clusters.created < ?
            """,
            (embed_model, chain_start, window_start),
        ):
            prior_vectors[row["cluster_id"]].append(unpack_vector(row["vector"]))
        prior_centroids = {
            cid: np.mean(vecs, axis=0) for cid, vecs in prior_vectors.items() if vecs
        }

        for new_cluster_id, member_vectors in new_cluster_vectors.items():
            if not member_vectors or not prior_centroids:
                continue
            centroid = np.mean(member_vectors, axis=0)
            best_cid, best_sim = None, 0.0
            for cid, prior_centroid in prior_centroids.items():
                sim = _cosine(centroid, prior_centroid)
                if sim > best_sim:
                    best_sim, best_cid = sim, cid
            if best_cid is not None and best_sim > chain_threshold:
                conn.execute(
                    "UPDATE clusters SET parent_cluster_id = ? WHERE id = ?",
                    (best_cid, new_cluster_id),
                )
                stats["chained"] += 1

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
            SELECT url_hash, feed_id, title, url, summary, country, topics, score, published, fetched, image
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

    distinct_countries = {
        c for r in rows for c in (r["country"] or "").split(",") if c
    }
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
        f"{stats['new_cluster_members']} article(s), "
        f"{stats['chained']} linked to an earlier developing story."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
