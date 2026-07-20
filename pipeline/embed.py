"""Summary embeddings, used by pipeline.cluster (story grouping) and
pipeline.dedup_predictions (near-duplicate predictions).

For each article with processed_status IN ('summarised', 'done') that
doesn't yet have a row in article_embeddings for the configured model:
  1. Embed its (English -- translated articles already have an English
     summary by this point, see pipeline.summarize) summary text.
  2. Store the vector in article_embeddings, keyed by (url_hash, model) so
     re-running is a no-op once every article has a row, and switching
     models later just adds new rows alongside old ones rather than
     requiring a migration.

Uses fastembed (ONNX Runtime) rather than the sentence-transformers package
itself -- same all-MiniLM-L6-v2 model, but without pulling in torch, which
is a ~500MB CUDA-enabled wheel by default on Linux vs. fastembed's ~20MB of
extra dependencies. Matters here because this runs 6x/day on a shared
Actions runner (.github/workflows/pipeline.yml).

Usage: python -m pipeline.embed
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np

from pipeline.config import get_config, resolve_path
from pipeline.db import get_connection, init_db

VECTOR_DTYPE = np.float32


def pack_vector(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=VECTOR_DTYPE).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=VECTOR_DTYPE)


@lru_cache(maxsize=1)
def get_embedder(model_name: str):
    """Cached per (process, model_name) so repeated calls in the same run
    don't reload the ONNX model. Import is local so modules that only need
    pack_vector/unpack_vector (e.g. pipeline.cluster reading existing
    vectors) don't require fastembed to be installed at import time."""
    from fastembed import TextEmbedding

    cache_dir = str(resolve_path("data/model_cache"))
    return TextEmbedding(model_name=model_name, cache_dir=cache_dir)


def embed_texts(texts: list[str], model_name: str | None = None) -> list[np.ndarray]:
    if model_name is None:
        model_name = get_config()["embed"]["model"]
    embedder = get_embedder(model_name)
    return [np.asarray(v, dtype=VECTOR_DTYPE) for v in embedder.embed(texts)]


def process_all(limit: int | None = None) -> dict:
    cfg = get_config()
    embed_cfg = cfg["embed"]
    model_name = embed_cfg["model"]
    if limit is None:
        limit = embed_cfg.get("max_embeddings_per_run", 200)

    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT articles.url_hash, articles.summary
                FROM articles
                LEFT JOIN article_embeddings
                    ON article_embeddings.url_hash = articles.url_hash
                    AND article_embeddings.model = ?
                WHERE articles.processed_status IN ('summarised', 'done')
                  AND articles.summary IS NOT NULL
                  AND article_embeddings.url_hash IS NULL
                ORDER BY articles.fetched
                LIMIT ?
                """,
                (model_name, limit),
            )
        ]

    stats = {"embedded": 0, "errors": 0}
    if not rows:
        return stats

    try:
        vectors = embed_texts([r["summary"] for r in rows], model_name)
    except Exception as exc:
        stats["errors"] = len(rows)
        print(f"  [error] embedding batch failed: {exc}", file=sys.stderr)
        return stats

    created = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        for row, vector in zip(rows, vectors):
            conn.execute(
                """
                INSERT OR REPLACE INTO article_embeddings (url_hash, model, vector, created)
                VALUES (?, ?, ?, ?)
                """,
                (row["url_hash"], model_name, pack_vector(vector), created),
            )
    stats["embedded"] = len(rows)
    return stats


def main() -> dict:
    init_db()
    print("Embedding summarised articles...")
    stats = process_all()
    print(f"Done: {stats['embedded']} embedded, {stats['errors']} errors.")
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
