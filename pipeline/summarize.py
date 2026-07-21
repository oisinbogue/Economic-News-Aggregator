"""Cerebras summarisation + translation layer.

For each article with processed_status='fetched':
  1. If the source feed's language isn't English, translate title+body to
     English via a Cerebras chat completion (original_title/original_raw_text
     are kept so the archive/search stays queryable in the source language --
     see brief feature #7).
  2. Generate a 2-3 sentence summary. Prompt content is ported verbatim from
     v1's generate_summary (aggregator.py:847-894) -- only the call target
     changed, from a local Ollama URL to the Cerebras API.
  3. Reject summaries that look like an LLM refusal/hedge and retry once.
     v1's companion `feed_report.py` (mentioned in the brief) isn't present
     on disk, so this check is a fresh implementation of the same idea
     rather than a port.
  4. Advance processed_status to 'summarised', or 'error' if every attempt
     fails, so a later run can find and retry it without reprocessing
     everything else.

Rate limiting is handled by pipeline.cerebras (shared with pipeline.curate).

Usage: python -m pipeline.summarize
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import httpx

from pipeline.cerebras import call as cerebras_call, get_api_key, load_dotenv
from pipeline.config import get_config
from pipeline.db import get_connection, init_db

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


def generate_summary(client: httpx.Client, api_key: str, model: str, title: str, body: str) -> str:
    """2-3 sentence summary. Prompt ported from v1's generate_summary
    (aggregator.py:847-894); everything but the call target is unchanged."""
    if not body:
        return "No article body text was available to summarise."

    prompt = (
        "Summarise the following news article in exactly 2 to 3 concise sentences. "
        "Focus on the key economic or policy insight. "
        "Write in plain prose — no bullet points, no headings.\n\n"
        f"Title: {title}\n\n"
        f"Article text:\n{body[:1500]}"
    )

    # gpt-oss-120b spends part of this budget on hidden reasoning before it
    # writes the visible summary (see pipeline.cerebras.call) -- 900 leaves
    # generous headroom over the ~150 tokens a 2-3 sentence summary needs.
    summary = cerebras_call(client, api_key, model, prompt, max_tokens=900)
    if contains_refusal_phrase(summary):
        summary = cerebras_call(client, api_key, model, prompt, max_tokens=900)
    return summary


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

    stats = {"processed": 0, "translated": 0, "errors": 0}
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

                summary = generate_summary(client, api_key, model, title, body)

                with get_connection() as conn:
                    conn.execute(
                        """
                        UPDATE articles
                        SET title = ?, original_title = ?, raw_text = ?,
                            original_raw_text = ?, summary = ?, language = ?,
                            processed_status = 'summarised'
                        WHERE url_hash = ?
                        """,
                        (title, original_title, body, original_raw_text, summary,
                         lang, row["url_hash"]),
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
    print("Summarising fetched articles via Cerebras...")
    started = datetime.now(timezone.utc)
    stats = process_all()
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    print(
        f"Done in {duration:.1f}s: {stats['processed']} summarised "
        f"({stats['translated']} translated), {stats['errors']} errors."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
