"""TAGGING_SPEC.md Phase 3 -- zero-shot topic backfill for the pre-LLM corpus.

pipeline.summarize (Phase 2) only tags newly-summarised rows with an LLM
call. Re-running every pre-existing row through it is not an option -- at
Cerebras's free-tier throttle of one call/13s, thousands of backlog articles
is many hours of Actions time. This script instead reuses the summary
embeddings pipeline.embed already computes for clustering: one hand-written
prototype sentence per config/taxonomy.yaml theme (see THEME_PROTOTYPES
below), embedded with the same model as article summaries, cosine-matched
against each article's summary vector, thresholded.

**Threshold calibration.** Rather than guessing a cosine cut-off, this script
calibrates against pipeline.summarize's own output: articles that already
carry tag_source='llm' have both an LLM-assigned topic set and (once
pipeline.embed has run) a summary embedding, so the pair is a ground-truth
example of "what should this cosine score have produced?". For each
candidate threshold in a fine grid, every LLM-tagged article's predicted
topic set (theme prototypes scoring above that threshold) is compared to its
actual LLM topic set via per-article Jaccard similarity (correct on both
what a topic list is -- multi-label -- and on the empty-topics case, where an
article that declined every theme should predict no themes to score well).
The threshold with the highest mean Jaccard across the LLM-tagged overlap is
selected and printed/logged; there is no hardcoded cut-off in this file
because the corpus (and therefore the right threshold) keeps growing as more
Phase 2 output accumulates.

Last calibration run (2026-08-06, local dev DB, 59 freshly-summarised
articles bootstrapped for this run since this checkout's DB started with
zero tag_source='llm' rows -- production's larger, organically-grown set
lives in the CI-managed database, not here):
  threshold=0.25, mean Jaccard agreement=0.553 over 59 LLM-tagged rows.
Re-run this script periodically as more LLM tags accumulate in production --
the printed calibration line is the number to trust, not this comment. A
59-row sample is a thin calibration set; expect the chosen threshold to
settle as production's tag_source='llm' population (thousands of rows,
accumulating a handful at a time under Phase 2) grows.

Only rows where tag_source IS NULL or 'keyword' AND a summary embedding
already exists (from pipeline.embed) are touched; tag_source='llm' rows are
never read for anything but calibration ground truth, let alone overwritten.
Written rows get tag_source='embedding'. country and score are untouched --
Phase 3 is topics only.

Usage: python -m scripts.backfill_topics_embedding [--dry-run] [--threshold N]
  --dry-run     Calibrate and report, but do not write to the database.
  --threshold   Skip calibration and use this cosine cut-off instead (for
                re-running with a previously-printed value without paying
                the calibration cost again).
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from pipeline.config import get_config
from pipeline.db import get_connection, init_db
from pipeline.embed import embed_texts, unpack_vector
from pipeline.tag import load_themes

# One representative sentence per config/taxonomy.yaml theme, written in the
# same register as the article summaries they're compared against (short,
# factual, economics-journalism prose) rather than a bare keyword list --
# sentence-transformers embeddings are trained on natural sentences.
THEME_PROTOTYPES = {
    "IRELAND": (
        "Ireland's economy, including its budget, national debt, housing "
        "market, inflation, GDP and fiscal policy."
    ),
    "HOUSING & PROPERTY": (
        "Housing and property markets, including house prices, rents, "
        "mortgage rates, housing supply and homelessness."
    ),
    "DEMOGRAPHICS & MIGRATION": (
        "Population demographics and migration, including birth rates, an "
        "ageing population, emigration, immigration and labour shortages."
    ),
    "MACROECONOMICS": (
        "Macroeconomic conditions, including inflation, interest rates, GDP "
        "growth, recession risk, government debt and central bank policy."
    ),
    "TRADE & GLOBALISATION": (
        "International trade and globalisation, including tariffs, trade "
        "wars, supply chains, foreign investment and multinational "
        "corporations."
    ),
    "INEQUALITY & LABOUR": (
        "Economic inequality and the labour market, including wages, "
        "unemployment, the gig economy, trade unions and the wealth gap."
    ),
    "EUROPE & ECB": (
        "The European Union and European Central Bank, including eurozone "
        "inflation, ECB interest rate decisions and EU fiscal policy."
    ),
    "GLOBAL ECONOMY": (
        "The global economy, including major national economies, commodity "
        "prices, currency markets, the Federal Reserve and international "
        "institutions like the IMF and World Bank."
    ),
}

# Calibration grid. 0.01 steps are finer than the cosine similarities
# between two MiniLM sentence embeddings meaningfully separate at, but cheap
# to compute since scoring 8 dot products per article is instant.
THRESHOLD_GRID = np.round(np.arange(0.05, 0.61, 0.01), 2)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom else 0.0


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def get_theme_vectors(model_name: str) -> dict[str, np.ndarray]:
    themes = load_themes()
    prototypes = [THEME_PROTOTYPES[t] for t in themes]
    vectors = embed_texts(prototypes, model_name)
    return dict(zip(themes, vectors))


def score_topics(vector: np.ndarray, theme_vectors: dict[str, np.ndarray], threshold: float) -> list[str]:
    return sorted(theme for theme, proto in theme_vectors.items() if cosine(vector, proto) > threshold)


def calibrate(theme_vectors: dict[str, np.ndarray], calibration_rows: list[tuple]) -> tuple[float, float]:
    """Returns (best_threshold, mean_jaccard_at_best_threshold)."""
    best_threshold, best_score = float(THRESHOLD_GRID[0]), -1.0
    for threshold in THRESHOLD_GRID:
        total = 0.0
        for vector, llm_topics in calibration_rows:
            predicted = set(score_topics(vector, theme_vectors, threshold))
            total += jaccard(predicted, llm_topics)
        mean_score = total / len(calibration_rows)
        if mean_score > best_score:
            best_threshold, best_score = float(threshold), mean_score
    return best_threshold, best_score


def fetch_calibration_rows(model_name: str) -> list[tuple[np.ndarray, set]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT articles.topics, article_embeddings.vector
            FROM articles
            JOIN article_embeddings
                ON article_embeddings.url_hash = articles.url_hash
                AND article_embeddings.model = ?
            WHERE articles.tag_source = 'llm'
            """,
            (model_name,),
        ).fetchall()
    return [
        (unpack_vector(row["vector"]), {t for t in (row["topics"] or "").split(",") if t})
        for row in rows
    ]


def fetch_target_rows(model_name: str) -> list[tuple[str, np.ndarray]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT articles.url_hash, article_embeddings.vector
            FROM articles
            JOIN article_embeddings
                ON article_embeddings.url_hash = articles.url_hash
                AND article_embeddings.model = ?
            WHERE articles.tag_source IS NULL OR articles.tag_source = 'keyword'
            """,
            (model_name,),
        ).fetchall()
    return [(row["url_hash"], unpack_vector(row["vector"])) for row in rows]


def untagged_share() -> tuple[int, int]:
    """Returns (untagged_count, total_count). 'Untagged' matches pipeline.tag's
    own NULL/'' distinction: NULL means never tagged, '' means tagged with no
    theme matched -- both count as untagged for this report."""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        untagged = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE topics IS NULL OR topics = ''"
        ).fetchone()[0]
    return untagged, total


def run(dry_run: bool, forced_threshold: float | None) -> int:
    init_db()
    model_name = get_config()["embed"]["model"]
    theme_vectors = get_theme_vectors(model_name)

    if forced_threshold is not None:
        threshold, agreement = forced_threshold, None
        print(f"Using forced threshold={threshold} (skipping calibration).")
    else:
        calibration_rows = fetch_calibration_rows(model_name)
        if not calibration_rows:
            print(
                "No tag_source='llm' rows with embeddings found -- nothing to "
                "calibrate against yet. Run pipeline.summarize and pipeline.embed "
                "first, or pass --threshold to skip calibration.",
                file=sys.stderr,
            )
            return 1
        threshold, agreement = calibrate(theme_vectors, calibration_rows)
        print(
            f"Calibrated threshold={threshold} against {len(calibration_rows)} "
            f"tag_source='llm' row(s), mean Jaccard agreement={agreement:.3f}"
        )

    before_untagged, total = untagged_share()

    target_rows = fetch_target_rows(model_name)
    updates = [(",".join(score_topics(vector, theme_vectors, threshold)), url_hash) for url_hash, vector in target_rows]
    non_empty = sum(1 for topics, _ in updates if topics)

    print(f"{len(updates)} row(s) eligible for embedding backfill (tag_source NULL/'keyword' with a summary embedding).")
    print(f"  -> {non_empty} matched at least one theme above threshold; {len(updates) - non_empty} matched none.")

    if dry_run:
        print("--dry-run: no changes written.")
        after_untagged = before_untagged - non_empty
    else:
        with get_connection() as conn:
            conn.executemany(
                "UPDATE articles SET topics = ?, tag_source = 'embedding' WHERE url_hash = ?",
                updates,
            )
        after_untagged, total = untagged_share()

    def pct(n: int) -> str:
        return f"{n}/{total} ({n / total:.0%})" if total else f"{n}/0"

    print(f"Untagged share: {pct(before_untagged)} -> {pct(after_untagged)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Calibrate and report but do not write to the database.")
    parser.add_argument("--threshold", type=float, default=None, help="Skip calibration and use this cosine cut-off.")
    args = parser.parse_args()
    return run(args.dry_run, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
