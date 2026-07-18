"""Country + topic tagging via config/taxonomy.yaml (brief feature #2).

For each article with processed_status='summarised' and topics IS NULL:
  - country: inherited directly from the source feed's declared country
    (config/feeds.yaml -> feeds.country) -- the feed already tells us where
    it's from, no NLP needed.
  - topics: matched against config/taxonomy.yaml's active keyword lists,
    using the same word-boundary/substring matching as v1's score_entry
    (aggregator.py:489-522). Every theme whose keywords appear in the
    article's title+summary+body is kept (comma-separated); '' if none
    matched, distinct from NULL so the row isn't reprocessed forever.
  - score: count of matched keywords, also ported from score_entry. Not
    used to rank the daily top 10 (that's LLM-curated -- brief feature #3),
    but used by pipeline.cluster to rank members within a cluster.

Usage: python -m pipeline.tag
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache

import yaml

from pipeline.config import get_config, resolve_path
from pipeline.db import get_connection, init_db


@lru_cache(maxsize=1)
def load_keywords() -> list[dict]:
    cfg = get_config()
    path = resolve_path(cfg["paths"]["taxonomy_yaml"])
    with open(path, "r", encoding="utf-8") as f:
        taxonomy = yaml.safe_load(f)

    keywords = []
    for theme, groups in (taxonomy.get("keywords") or {}).items():
        for kw in groups.get("active") or []:
            keywords.append({"keyword": kw.lower(), "theme": theme})
    return keywords


def score_and_tag(blob: str, keywords: list[dict]) -> tuple[int, list[str]]:
    """Ported from aggregator.py:489-522 (score_entry): word-boundary match
    for single words so "rate" doesn't match "moderate", substring match for
    multi-word phrases."""
    blob = (blob or "").lower()
    matched_themes: set[str] = set()
    matched = 0
    for kw in keywords:
        word = kw["keyword"]
        if " " in word:
            if word in blob:
                matched_themes.add(kw["theme"])
                matched += 1
        elif re.search(r"\b" + re.escape(word) + r"\b", blob):
            matched_themes.add(kw["theme"])
            matched += 1
    return matched, sorted(matched_themes)


def process_all() -> dict:
    keywords = load_keywords()

    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT articles.url_hash, articles.title, articles.summary,
                       articles.raw_text, feeds.country AS feed_country
                FROM articles
                JOIN feeds ON feeds.id = articles.feed_id
                WHERE articles.processed_status = 'summarised'
                  AND articles.topics IS NULL
                """
            )
        ]

    stats = {"tagged": 0}
    with get_connection() as conn:
        for row in rows:
            blob = " ".join(filter(None, [row["title"], row["summary"], row["raw_text"]]))
            score, themes = score_and_tag(blob, keywords)
            conn.execute(
                "UPDATE articles SET country = ?, topics = ?, score = ? WHERE url_hash = ?",
                (row["feed_country"], ",".join(themes), score, row["url_hash"]),
            )
            stats["tagged"] += 1

    return stats


def main() -> dict:
    init_db()
    print("Tagging summarised articles (country + topics + score)...")
    stats = process_all()
    print(f"Done: {stats['tagged']} article(s) tagged.")
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
