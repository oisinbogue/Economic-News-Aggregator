"""Cerebras summarisation + translation layer.

For each article with processed_status='fetched':
  1. If the source feed's language isn't English, translate title+body to
     English via a Cerebras chat completion (original_title/original_raw_text
     are kept so the archive/search stays queryable in the source language --
     see brief feature #7).
  2. Generate a 2-3 sentence summary *and* the article's topics and primary
     country, on one call (see summarise_and_tag). The summary half of the
     prompt is ported verbatim from v1's generate_summary
     (aggregator.py:847-894); the tagging half is TAGGING_SPEC.md Phase 2.
  3. Reject summaries that look like an LLM refusal/hedge and retry once.
     v1's companion `feed_report.py` (mentioned in the brief) isn't present
     on disk, so this check is a fresh implementation of the same idea
     rather than a port.
  4. Advance processed_status to 'summarised', or 'error' if every attempt
     fails, so a later run can find and retry it without reprocessing
     everything else.

Tagging rides along on step 2's call rather than getting its own, because the
Cerebras free tier throttles on *requests*, not tokens (pipeline.cerebras --
one call per 13s process-wide). Asking for topics and a country in the same
completion costs a handful of extra output tokens and zero extra rate-limit
budget; a second call per article would roughly double the pipeline's wall
clock. Rows whose tag block parses are written with tag_source='llm' and
non-NULL topics, which is exactly the condition pipeline.tag's query skips --
so the keyword tagger there degrades naturally into a parse-failure fallback
without either module needing to know about the other's outcome.

Rate limiting is handled by pipeline.cerebras (shared with pipeline.curate).

Usage: python -m pipeline.summarize
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import NamedTuple

import httpx

from pipeline.cerebras import call as cerebras_call, get_api_key, load_dotenv
from pipeline.config import get_config
from pipeline.db import get_connection, init_db
from pipeline.geo import detect_countries
from pipeline.tag import load_keywords, load_themes, score_and_tag

REFUSAL_PHRASES = (
    "as an ai language model",
    "as a language model",
    "i cannot provide",
    "i can't provide",
    "i cannot assist",
    "i can't assist",
    "i cannot fulfill",
    "i can't fulfill",
    "i'm not able to",
    "i am not able to",
    "i don't have access to",
    "i do not have access to",
    "i'm sorry, but i",
    "i am sorry, but i",
    "i'm unable to",
    "i am unable to",
)


def contains_refusal_phrase(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in REFUSAL_PHRASES)


# The model is told to write this when no theme fits and when none of the
# country candidates is what the article is actually about. An explicit
# "decline" token matters: without one the only way to answer is to pick
# something, which is precisely the guessing the keyword tagger already did.
NONE_TOKEN = "NONE"

# gpt-oss-120b draws hidden reasoning tokens from the same budget as the
# visible answer and, when it runs out, returns *no content at all* rather
# than a truncated answer (see pipeline.cerebras.call). The summary-only
# prompt allowed 900. Classifying against 8 themes plus a candidate list is
# markedly more "thinking" per call than summarising alone, while the extra
# visible output is only ~20 tokens -- so the headroom, not the answer, is
# what has to grow. 2400 stays well inside the 8,192-token context once the
# ~700-token prompt is accounted for.
MAX_TOKENS = 2400


class Analysis(NamedTuple):
    """`topics is None` means the tag block did not parse and pipeline.tag's
    keyword fallback should be left to handle the row -- distinct from
    `topics == []`, which is the model explicitly declining to assign one."""

    summary: str
    topics: list[str] | None
    country: str | None


def build_prompt(title: str, body: str, themes: tuple[str, ...], candidates: list[str]) -> str:
    theme_lines = "\n".join(f"- {t}" for t in themes)
    if candidates:
        candidate_lines = "\n".join(f"- {c}" for c in candidates)
        country_instruction = (
            "COUNTRY: which single country or region the article is mainly "
            "about. Choose exactly one from this candidate list, copied "
            f"exactly as written:\n{candidate_lines}\n"
            "The list was produced by scanning the text for place names, so it "
            "includes places mentioned only in passing. Pick the one the "
            f"article is genuinely about. Write {NONE_TOKEN} if none of them is.\n"
        )
    else:
        # No candidates at all -- asking the model to choose from an empty
        # list invites it to invent a bucket that isn't in countries.yaml.
        country_instruction = f"COUNTRY: write {NONE_TOKEN}.\n"

    return (
        "You are summarising and tagging a news article for an economics news "
        "aggregator. Respond in exactly this format, with no extra text before "
        "or after:\n"
        "SUMMARY: <your summary>\n"
        "TOPICS: <your topics>\n"
        "COUNTRY: <your country>\n\n"
        "SUMMARY: summarise the article in exactly 2 to 3 concise sentences. "
        "Focus on the key economic or policy insight. "
        "Write in plain prose — no bullet points, no headings.\n\n"
        "TOPICS: list every theme below that the article is substantially "
        "about, separated by commas. Choose only from this list, copied "
        f"exactly as written:\n{theme_lines}\n"
        "A theme merely being mentioned in passing does not count. If none of "
        f"them genuinely fits, write {NONE_TOKEN} — do not guess.\n\n"
        f"{country_instruction}\n"
        f"Title: {title}\n\n"
        f"Article text:\n{body[:1500]}"
    )


def parse_response(raw: str, themes: tuple[str, ...], candidates: list[str]) -> Analysis | None:
    """None means even the summary is unrecoverable, so the caller should
    retry the whole call. A non-None result with topics=None means the
    summary is usable but the tags are not."""
    if "SUMMARY:" not in raw:
        return None
    _, _, rest = raw.partition("SUMMARY:")
    summary_part, topics_marker, rest = rest.partition("TOPICS:")
    summary = summary_part.strip()
    if not summary:
        return None
    if not topics_marker:
        return Analysis(summary, None, None)

    topics_part, _, country_part = rest.partition("COUNTRY:")

    # Compare case-insensitively but store the taxonomy's own spelling, so a
    # model that lowercases "Housing & Property" still lands on a theme name
    # the site's filters and config/taxonomy.yaml agree on.
    by_upper = {t.upper(): t for t in themes}
    topics: list[str] | None = []
    saw_valid_label = False
    for raw_topic in topics_part.replace("\n", ",").split(","):
        label = raw_topic.strip().strip("-*.").strip()
        if not label:
            continue
        if label.upper() == NONE_TOKEN:
            saw_valid_label = True
            continue
        canonical = by_upper.get(label.upper())
        if canonical is None:
            # Outside the closed set -- dropped, not coerced to a near match.
            continue
        saw_valid_label = True
        if canonical not in topics:
            topics.append(canonical)
    if not saw_valid_label:
        # The model neither picked a real theme nor declined -- we cannot
        # tell "no theme fits" from "it ignored the list", so treat the tag
        # block as unparsed rather than recording a confident empty answer.
        topics = None

    country = None
    chosen = country_part.strip().splitlines()[0].strip().strip("-*.").strip() if country_part.strip() else ""
    if chosen and chosen.upper() != NONE_TOKEN:
        for candidate in candidates:
            if candidate.lower() == chosen.lower():
                country = candidate
                break

    return Analysis(summary, topics, country)


def rank_countries(chosen: str | None, candidates: list[str]) -> list[str]:
    """The LLM's pick is promoted to position 0 -- which is what
    pipeline.curate.dominant_country reads -- while the rest of Phase 1's
    ranking is kept behind it, so the site's country filters lose no recall."""
    if not chosen:
        return list(candidates)
    return [chosen] + [c for c in candidates if c != chosen]


def summarise_and_tag(
    client: httpx.Client, api_key: str, model: str, title: str, body: str, candidates: list[str]
) -> Analysis:
    """One Cerebras call for summary + topics + primary country."""
    if not body:
        return Analysis("No article body text was available to summarise.", None, None)

    themes = load_themes()
    prompt = build_prompt(title, body, themes, candidates)

    result = None
    for _ in range(2):
        raw = cerebras_call(client, api_key, model, prompt, max_tokens=MAX_TOKENS)
        result = parse_response(raw, themes, candidates)
        # Retry once on a refusal (pre-existing behaviour) and on a tag block
        # that won't parse -- one extra call is the same cost the refusal
        # path already accepted, and it is bounded at one either way.
        if result is not None and result.topics is not None and not contains_refusal_phrase(result.summary):
            return result

    if result is not None:
        return result
    # No SUMMARY: marker twice running -- the model answered in prose instead
    # of the requested format. That prose is almost always a perfectly good
    # summary, so keep it and let pipeline.tag's keyword fallback supply the
    # tags, rather than failing the article outright over formatting.
    return Analysis(raw.strip(), None, None)


def translate_to_english(client: httpx.Client, api_key: str, model: str, title: str, body: str) -> tuple[str, str]:
    """Translates title+body to English via the same Cerebras call pattern
    used for summarisation. Returns (english_title, english_body)."""
    prompt = (
        "Translate the following news article title and body into English. "
        "Preserve meaning and factual content exactly -- do not summarise, add "
        "commentary, or omit anything. Respond in exactly this format with no "
        "extra text before or after:\n"
        "TITLE: <translated title>\n"
        "BODY: <translated body>\n\n"
        f"Title: {title}\n\n"
        f"Body:\n{(body or '')[:4000]}"
    )
    # Translated body can run to ~4000 chars (~1000 tokens) plus reasoning
    # overhead -- 3000 leaves headroom on top of that.
    raw = cerebras_call(client, api_key, model, prompt, max_tokens=3000)

    if "TITLE:" in raw and "BODY:" in raw:
        _, _, after_title = raw.partition("TITLE:")
        title_part, _, body_part = after_title.partition("BODY:")
        return title_part.strip(), body_part.strip()
    # Model didn't follow the format -- fall back to the untranslated text
    # rather than risk storing garbage as if it were a clean translation.
    return title, body


def process_all(limit: int | None = None) -> dict:
    cfg = get_config()
    model = cfg["llm"]["model"]
    api_key = get_api_key()
    if limit is None:
        limit = cfg["run"].get("max_summaries_per_run", 60)

    with get_connection() as conn:
        # Newest-fetched-first (not FIFO): a normal cycle's new arrivals are
        # small enough to always fit under `limit`, so today's articles never
        # wait behind an older backlog -- any leftover budget still drains
        # the backlog, just from most- to least-recently-stuck.
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT articles.url_hash, articles.title, articles.raw_text,
                       feeds.language AS feed_language
                FROM articles
                JOIN feeds ON feeds.id = articles.feed_id
                WHERE articles.processed_status = 'fetched'
                ORDER BY articles.fetched DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]

    stats = {"processed": 0, "translated": 0, "errors": 0, "llm_tagged": 0}
    if not rows:
        return stats

    with httpx.Client() as client:
        for row in rows:
            title, body, lang = row["title"], row["raw_text"], row["feed_language"]
            original_title = None
            original_raw_text = None
            try:
                if lang and lang.strip().lower() not in ("en", "eng"):
                    original_title, original_raw_text = title, body
                    title, body = translate_to_english(client, api_key, model, title, body)
                    stats["translated"] += 1

                # Phase 1's ranked candidates, detected on the English text so
                # the gazetteer sees what it was built for. Title first, since
                # detect_countries weights the leading characters heaviest.
                candidates = detect_countries(" ".join(filter(None, [title, body])))
                analysis = summarise_and_tag(client, api_key, model, title, body, candidates)

                if analysis.topics is None:
                    # Tags unusable -- leave topics NULL so pipeline.tag picks
                    # the row up and applies the keyword fallback.
                    tag_columns = ""
                    tag_values: tuple = ()
                else:
                    # score_and_tag's *themes* are superseded here, but its
                    # keyword count still populates articles.score, which
                    # pipeline.cluster ranks members on until TAGGING_SPEC.md
                    # Phase 4 swaps it for centroid distance. Leaving score
                    # NULL on LLM rows would sort them last in that ORDER BY.
                    blob = " ".join(filter(None, [title, analysis.summary, body]))
                    score, _ = score_and_tag(blob, load_keywords())
                    tag_columns = "country = ?, topics = ?, score = ?, tag_source = 'llm',"
                    ranked = rank_countries(analysis.country, candidates)
                    tag_values = (",".join(ranked) or "International", ",".join(analysis.topics), score)
                    stats["llm_tagged"] += 1

                with get_connection() as conn:
                    conn.execute(
                        f"""
                        UPDATE articles
                        SET title = ?, original_title = ?, raw_text = ?,
                            original_raw_text = ?, summary = ?, language = ?,
                            {tag_columns}
                            processed_status = 'summarised'
                        WHERE url_hash = ?
                        """,
                        (title, original_title, body, original_raw_text, analysis.summary,
                         lang, *tag_values, row["url_hash"]),
                    )
                stats["processed"] += 1
            except Exception as exc:
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE articles SET processed_status = 'error' WHERE url_hash = ?",
                        (row["url_hash"],),
                    )
                stats["errors"] += 1
                print(f"  [error] {row['url_hash'][:12]}: {exc}", file=sys.stderr)

    return stats


def main() -> dict:
    load_dotenv()
    init_db()
    print("Summarising + tagging fetched articles via Cerebras...")
    started = datetime.now(timezone.utc)
    stats = process_all()
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    print(
        f"Done in {duration:.1f}s: {stats['processed']} summarised "
        f"({stats['translated']} translated, {stats['llm_tagged']} LLM-tagged), "
        f"{stats['errors']} errors."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
