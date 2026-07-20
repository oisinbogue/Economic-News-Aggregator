"""Prediction dedup within merged clusters (brief feature #4).

pipeline.predictions extracts one prediction per article, before that
article has been clustered (see .github/workflows/pipeline.yml -- fetch ->
summarize -> predictions -> tag -> cluster -> dedup_predictions -> ...).
Once several articles about the same story land in one cluster, they can
each yield their own paraphrased prediction of the same underlying claim
(same who/what/direction/horizon, different wording). This pass runs after
pipeline.cluster and collapses those near-duplicates into one canonical
prediction, using the same claim-embedding approach as pipeline.embed
rather than a separate similarity mechanism.

Design (idempotent/resumable, same spirit as the rest of the pipeline):
  - A prediction is a "candidate" if predictions.canonical_id IS NULL (i.e.
    not already collapsed into another prediction) AND its source article
    has a cluster_id.
  - For each cluster with >= 2 candidates, walk them oldest-first. Compare
    each one's claim embedding against the claim embeddings of candidates
    already accepted as canonical earlier in that walk; if the best match
    clears `predictions.dedup_cosine_threshold`, set this prediction's
    canonical_id to that earlier one's id instead of leaving it NULL.
  - Re-running is safe: already-collapsed predictions (canonical_id set)
    are excluded from the candidate set, so a later run only has to compare
    newly-extracted predictions against the surviving canonical ones --
    nothing is re-decided.
  - Nothing is deleted. A canonical prediction's full set of source
    articles is recoverable with
    `SELECT source FROM predictions WHERE id = ? OR canonical_id = ?`.

Usage: python -m pipeline.dedup_predictions
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone

from pipeline.config import get_config
from pipeline.db import get_connection, init_db
from pipeline.embed import embed_texts, pack_vector, unpack_vector


def _cosine(a, b) -> float:
    import numpy as np

    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _ensure_embeddings(conn, predictions: list[dict], model: str) -> dict[int, "object"]:
    """Returns {prediction_id: vector}, embedding+caching any predictions
    that don't already have a prediction_embeddings row for this model."""
    ids = [p["id"] for p in predictions]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    existing = {
        row["prediction_id"]: unpack_vector(row["vector"])
        for row in conn.execute(
            f"SELECT prediction_id, vector FROM prediction_embeddings "
            f"WHERE model = ? AND prediction_id IN ({placeholders})",
            (model, *ids),
        )
    }

    missing = [p for p in predictions if p["id"] not in existing]
    if missing:
        vectors = embed_texts([p["claim"] for p in missing], model)
        created = datetime.now(timezone.utc).isoformat()
        for p, vector in zip(missing, vectors):
            conn.execute(
                """
                INSERT OR REPLACE INTO prediction_embeddings (prediction_id, model, vector, created)
                VALUES (?, ?, ?, ?)
                """,
                (p["id"], model, pack_vector(vector), created),
            )
            existing[p["id"]] = vector

    return existing


def process_all() -> dict:
    cfg = get_config()
    embed_model = cfg["embed"]["model"]
    threshold = cfg["predictions"].get("dedup_cosine_threshold", 0.85)

    with get_connection() as conn:
        candidates = [
            dict(row)
            for row in conn.execute(
                """
                SELECT predictions.id, predictions.claim, predictions.logged_date,
                       articles.cluster_id
                FROM predictions
                JOIN articles ON articles.url_hash = predictions.source
                WHERE predictions.canonical_id IS NULL
                  AND articles.cluster_id IS NOT NULL
                ORDER BY predictions.logged_date ASC
                """
            )
        ]

    stats = {"clusters_checked": 0, "collapsed": 0}
    by_cluster: dict[int, list[dict]] = defaultdict(list)
    for c in candidates:
        by_cluster[c["cluster_id"]].append(c)

    clusters_with_multiple = {cid: rows for cid, rows in by_cluster.items() if len(rows) > 1}
    if not clusters_with_multiple:
        return stats

    with get_connection() as conn:
        vectors = _ensure_embeddings(conn, candidates, embed_model)

        for cluster_id, rows in clusters_with_multiple.items():
            stats["clusters_checked"] += 1
            canonicals: list[dict] = []  # accepted-as-canonical so far, this cluster
            for row in rows:
                v = vectors.get(row["id"])
                best_id, best_sim = None, 0.0
                if v is not None:
                    for canon in canonicals:
                        cv = vectors.get(canon["id"])
                        if cv is None:
                            continue
                        sim = _cosine(v, cv)
                        if sim > best_sim:
                            best_sim, best_id = sim, canon["id"]
                if best_id is not None and best_sim > threshold:
                    conn.execute(
                        "UPDATE predictions SET canonical_id = ? WHERE id = ?",
                        (best_id, row["id"]),
                    )
                    stats["collapsed"] += 1
                else:
                    canonicals.append(row)

    return stats


def main() -> dict:
    init_db()
    print("Deduping predictions within merged clusters...")
    stats = process_all()
    print(
        f"Done: {stats['clusters_checked']} cluster(s) with multiple predictions checked, "
        f"{stats['collapsed']} collapsed into an earlier canonical prediction."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
