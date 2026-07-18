"""LLM-curated daily top 10 (brief feature #3).

Replaces v1's keyword-count sort with a Cerebras judgement call on
significance -- v1's TOP_N=5 (aggregator.py:100) picked the highest keyword
`score`, which the brief explicitly says not to carry forward.

For "today" (UTC date):
  1. Candidate clusters = every cluster whose representative article was
     fetched within `cluster.window_days` days, most recent first, capped at
     `curate.max_candidates` (config.yaml) so the shortlist fits comfortably
     inside Cerebras's 8,192-token context in one call.
  2. Ask Cerebras to rank the 10 most significant for economic/policy impact
     and give a one-sentence reason for each.
  3. Replace today's rows in daily_top10 with the result, so re-running
     later in the day (this workflow runs several times a day) refreshes
     the picks as more of the day's news comes in, rather than freezing on
     whatever was known at the first run.

Usage: python -m pipeline.curate
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

from pipeline.cerebras import call as cerebras_call, get_api_key, load_dotenv
from pipeline.config import get_config
from pipeline.db import get_connection, init_db

RANK_LINE_RE = re.compile(r"RANK:\s*(\d+)\s*ID:\s*(\d+)\s*REASON:\s*(.+)", re.IGNORECASE)


def _load_candidates(conn, window_days: int, max_candidates: int) -> list[dict]:
    window_start = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT clusters.id AS cluster_id, articles.title, articles.summary
            FROM clusters
            JOIN articles ON articles.url_hash = clusters.representative_article
            WHERE articles.fetched >= ?
            ORDER BY articles.fetched DESC
            LIMIT ?
            """,
            (window_start, max_candidates),
        )
    ]


def build_prompt(candidates: list[dict]) -> str:
    listing = "\n".join(
        f"[{c['cluster_id']}] {c['title']} -- {(c['summary'] or '')[:200]}"
        for c in candidates
    )
    return (
        "You are curating a daily top-10 economic news digest from the "
        f"{len(candidates)} candidate stories below, each tagged with its "
        "cluster ID in brackets. Pick the 10 most significant for global "
        "economic or policy impact -- significance, not sensationalism. "
        "Respond with exactly 10 lines, most significant first, in exactly "
        "this format and no other text:\n"
        "RANK: <1-10> ID: <cluster id> REASON: <one concise sentence>\n\n"
        f"{listing}"
    )


def parse_ranking(raw: str, valid_ids: set[int]) -> list[tuple[int, int, str]]:
    """Returns [(rank, cluster_id, reason), ...], de-duplicated by cluster_id,
    dropping any hallucinated ID not in the candidate set, capped at 10."""
    results: list[tuple[int, int, str]] = []
    seen_ids: set[int] = set()
    for line in raw.splitlines():
        m = RANK_LINE_RE.search(line)
        if not m:
            continue
        rank, cluster_id, reason = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if cluster_id not in valid_ids or cluster_id in seen_ids:
            continue
        seen_ids.add(cluster_id)
        results.append((rank, cluster_id, reason))
    results.sort(key=lambda r: r[0])
    return results[:10]


def process_all() -> dict:
    cfg = get_config()
    model = cfg["llm"]["model"]
    api_key = get_api_key()
    window_days = cfg["cluster"].get("window_days", 3)
    max_candidates = cfg["curate"].get("max_candidates", 40)

    with get_connection() as conn:
        candidates = _load_candidates(conn, window_days, max_candidates)

    stats = {"candidates": len(candidates), "picked": 0}
    if not candidates:
        return stats

    prompt = build_prompt(candidates)
    with httpx.Client() as client:
        # gpt-oss-120b reasons through each candidate before writing the 10
        # output lines -- with max_candidates=40 that reasoning alone ran
        # ~700 tokens and got cut off before any output (see
        # pipeline.cerebras.call); 4000 leaves headroom and still fits
        # comfortably inside the 8,192-token context alongside the prompt.
        raw = cerebras_call(client, api_key, model, prompt, max_tokens=4000)

    valid_ids = {c["cluster_id"] for c in candidates}
    ranking = parse_ranking(raw, valid_ids)

    today = date.today().isoformat()
    with get_connection() as conn:
        conn.execute("DELETE FROM daily_top10 WHERE date = ?", (today,))
        for rank, cluster_id, reason in ranking:
            conn.execute(
                "INSERT INTO daily_top10 (date, rank, cluster_id, rationale) VALUES (?, ?, ?, ?)",
                (today, rank, cluster_id, reason),
            )
    stats["picked"] = len(ranking)
    return stats


def main() -> dict:
    load_dotenv()
    init_db()
    print("Curating daily top 10 via Cerebras...")
    stats = process_all()
    print(f"Done: {stats['picked']} of {stats['candidates']} candidate(s) picked for today's top 10.")
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
