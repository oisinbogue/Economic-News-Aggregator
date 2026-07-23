"""Country + topic tagging via config/taxonomy.yaml (brief feature #2).

For each article with processed_status='summarised' and topics IS NULL:
  - country: which country/region the article is actually ABOUT, detected
    from its text via pipeline.geo.detect_countries (spaCy NER + a
    places/institutions gazetteer, config/countries.yaml) -- not inherited
    from the source feed's own country the way this used to work, since a
    US outlet writing about the Irish housing market should tag
    country=Ireland, not country=United States. Comma-separated, an article
    can span several countries; "International" if detection found nothing
    specific to tag (a genuinely global-economy piece, or one this pass
    just missed) rather than falling back to feed location.
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
from pipeline.geo import detect_countries


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
    # spaCy NER is meaningfully heavier per-article than the pure regex/
    # substring matching this stage used to do -- bounded like every other
    # per-run-costed stage (max_summaries_per_run, max_embeddings_per_run) so
    # a large backlog can't blow the Actions job time budget in one run.
    limit = get_config().get("tag", {}).get("max_articles_per_run")

    query = (
        "SELECT url_hash, title, summary, raw_text FROM articles "
        "WHERE processed_status = 'summarised' AND topics IS NULL "
        "ORDER BY fetched DESC"
    )
    with get_connection() as conn:
        if limit:
            rows = [dict(row) for row in conn.execute(query + " LIMIT ?", (limit,))]
        else:
            rows = [dict(row) for row in conn.execute(query)]

    stats = {"tagged": 0}
    with get_connection() as conn:
        for row in rows:
            blob = " ".join(filter(None, [row["title"], row["summary"], row["raw_text"]]))
            score, themes = score_and_tag(blob, keywords)
            countries = detect_countries(blob) or ["International"]
            conn.execute(
                "UPDATE articles SET country = ?, topics = ?, score = ? WHERE url_hash = ?",
                (",".join(countries), ",".join(themes), score, row["url_hash"]),
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
