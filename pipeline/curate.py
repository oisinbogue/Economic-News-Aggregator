"""LLM-curated daily top 10 (brief feature #3).

Replaces v1's keyword-count sort with a Cerebras judgement call on
significance -- v1's TOP_N=5 (aggregator.py:100) picked the highest keyword
`score`, which the brief explicitly says not to carry forward.

For "today" (UTC date):
  1. Candidate clusters = every cluster whose representative article was
     fetched within `curate.candidate_window_hours` (config.yaml), most
     recent first, capped at `curate.max_candidates` so the shortlist fits
     comfortably inside Cerebras's 8,192-token context in one call. This is
     deliberately its own knob, not `cluster.window_days` (which exists to
     stop union-find linking unrelated stories months apart) -- if the 24h
     window returns fewer than `curate.min_candidates`, it's widened to 48h
     so the diversity caps in step 3 have enough of a pool to work with.
  2. Ask Cerebras for a JSON-ranked list of `curate.oversample_count`
     candidates (more than 10) with a one-sentence rationale each, validated
     against a Pydantic schema with one retry on malformed output. The
     oversample gives the diversity re-pick loop (step 3) room to skip
     cap-violating picks without running out of candidates.
  3. Greedily select 10 from that ranked list, enforcing hard per-day caps
     of `curate.max_per_country` and `curate.max_per_topic` -- this is a
     post-filter around the LLM call, not something the prompt asks the
     model to self-enforce (see `_select_diverse`).
  4. Replace today's rows in daily_top10 with the result, so re-running
     later in the day (this workflow runs several times a day) refreshes
     the picks as more of the day's news comes in, rather than freezing on
     whatever was known at the first run.

Usage: python -m pipeline.curate
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx
import yaml
from pydantic import BaseModel, ValidationError

from pipeline.cerebras import call as cerebras_call, get_api_key, load_dotenv
from pipeline.config import get_config, resolve_path
from pipeline.db import get_connection, init_db
from pipeline.timeutil import utc_today

# Diversity cap defaults (overridable via config.yaml's curate: section) --
# named here rather than inlined so config.yaml's comments are the single
# source of truth for current values, these are just the fallback.
DEFAULT_MAX_PER_COUNTRY = 3
DEFAULT_MAX_PER_TOPIC = 3
DEFAULT_OVERSAMPLE_COUNT = 20
DEFAULT_REPEAT_LOOKBACK_DAYS = 3
DEFAULT_CANDIDATE_WINDOW_HOURS = 24
DEFAULT_MIN_CANDIDATES = 25
# Fallback window used when the default window returns fewer than
# min_candidates candidates -- see process_all's thin-day widen step.
WIDENED_CANDIDATE_WINDOW_HOURS = 48

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class CuratorPick(BaseModel):
    rank: int
    cluster_id: int
    rationale: str


class CuratorOutput(BaseModel):
    picks: list[CuratorPick]


def _load_candidates(conn, window_hours: int, max_candidates: int) -> list[dict]:
    window_start = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT clusters.id AS cluster_id, articles.title, articles.summary,
                   articles.country, articles.topics
            FROM clusters
            JOIN articles ON articles.url_hash = clusters.representative_article
            WHERE articles.fetched >= ?
              AND (articles.published IS NULL OR articles.published >= ?)
            ORDER BY articles.fetched DESC
            LIMIT ?
            """,
            (window_start, window_start, max_candidates),
        )
    ]


def _load_candidates_with_fallback(
    conn, candidate_window_hours: int, min_candidates: int, max_candidates: int
) -> tuple[list[dict], bool]:
    """Loads candidates at `candidate_window_hours`; if that's thinner than
    `min_candidates`, re-queries at WIDENED_CANDIDATE_WINDOW_HOURS instead.
    Returns (candidates, widened) so callers can log when the fallback fired.
    See the widen-to-48h note in this module's docstring for why."""
    candidates = _load_candidates(conn, candidate_window_hours, max_candidates)
    if len(candidates) < min_candidates:
        return _load_candidates(conn, WIDENED_CANDIDATE_WINDOW_HOURS, max_candidates), True
    return candidates, False


def _recent_top10_cluster_ids(conn, lookback_days: int) -> set[int]:
    """Cluster IDs featured in daily_top10 in the `lookback_days` before
    today (today excluded -- this run is about to (re)write today's rows).
    Clusters persist across days (pipeline.cluster reattaches new articles
    to an existing cluster rather than always forming a new one), so
    cluster_id is a reliable "same story" identity for this check."""
    today = utc_today().isoformat()
    window_start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    rows = conn.execute(
        "SELECT DISTINCT cluster_id FROM daily_top10 WHERE date >= ? AND date < ?",
        (window_start, today),
    )
    return {row["cluster_id"] for row in rows}


def _load_theme_priority() -> list[str]:
    cfg = get_config()
    path = resolve_path(cfg["paths"]["taxonomy_yaml"])
    with open(path, "r", encoding="utf-8") as f:
        taxonomy = yaml.safe_load(f)
    return taxonomy.get("theme_priority") or []


def dominant_topic(topics_csv: str | None, theme_priority: list[str]) -> str | None:
    """Single topic tag used for the diversity cap. Mirrors
    pipeline.build.dominant_theme (same priority order, same tie-break) so
    the cap groups stories the same way the homepage does; kept as its own
    copy rather than an import so pipeline.curate doesn't pull in build.py's
    Jinja2/template dependencies for one small pure function."""
    topics = [t for t in (topics_csv or "").split(",") if t]
    if not topics:
        return None
    for theme in theme_priority:
        if theme in topics:
            return theme
    return sorted(topics)[0]


def dominant_country(countries_csv: str | None) -> str | None:
    """Single country tag used for the diversity cap. articles.country is now
    multi-valued (an article can be about several countries -- see
    pipeline.geo) and, since Phase 1, order-significant: ranked by evidence
    strength, most-relevant first. So this just takes position 0 rather than
    picking alphabetically, which used to let an incidental mention (e.g.
    "Australia/New Zealand" on an Ireland story) win the cap purely on
    spelling."""
    countries = [c for c in (countries_csv or "").split(",") if c]
    return countries[0] if countries else None


def build_prompt(candidates: list[dict], pick_count: int, recent_ids: set[int]) -> str:
    listing = "\n".join(
        f"[{c['cluster_id']}] {c['title']} -- {(c['summary'] or '')[:200]}"
        for c in candidates
    )
    recent_note = ""
    candidate_ids = {c["cluster_id"] for c in candidates}
    recent_present = sorted(i for i in recent_ids if i in candidate_ids)
    if recent_present:
        recent_note = (
            "\n\nThese cluster IDs were already featured in the top 10 within "
            f"the last few days: {', '.join(str(i) for i in recent_present)}. "
            "Deprioritize them unless the story has materially escalated or "
            "updated since -- don't exclude them outright, just don't re-run "
            "stale coverage.\n"
        )
    return (
        "You are curating a daily top-10 economic news digest from the "
        f"{len(candidates)} candidate stories below, each tagged with its "
        "cluster ID in brackets. Rank the "
        f"{pick_count} most significant for global economic or policy impact "
        "-- significance, not sensationalism, most significant first."
        f"{recent_note}\n"
        "Respond with ONLY valid JSON, no other text, in exactly this shape:\n"
        '{"picks": [{"rank": 1, "cluster_id": <id>, "rationale": "<one concise sentence>"}, ...]}\n'
        f"Include exactly {pick_count} entries, ranks 1 through {pick_count}.\n\n"
        f"{listing}"
    )


def _strip_code_fence(raw: str) -> str:
    return _CODE_FENCE_RE.sub("", raw.strip()).strip()


def _request_curation(
    client: httpx.Client, api_key: str, model: str, prompt: str, valid_ids: set[int], pick_count: int
) -> list[CuratorPick]:
    """Calls Cerebras, validates the JSON response against CuratorOutput,
    and retries once (with the validation error appended to the prompt) on
    malformed output before giving up. Hallucinated cluster IDs (not in
    valid_ids) and duplicate IDs are dropped rather than treated as a
    validation failure -- same tolerance the old regex parser had."""
    current_prompt = prompt
    last_error: Exception | None = None
    for attempt in range(2):
        # gpt-oss-120b reasons through each candidate before writing output --
        # with max_candidates=40 that reasoning alone ran ~700 tokens and got
        # cut off before any output (see pipeline.cerebras.call). 6000 leaves
        # headroom for the larger oversampled JSON response and still fits
        # comfortably inside the 8,192-token context alongside the prompt.
        raw = cerebras_call(client, api_key, model, current_prompt, max_tokens=6000)
        try:
            data = json.loads(_strip_code_fence(raw))
            output = CuratorOutput.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            current_prompt = (
                f"{prompt}\n\nYour previous response was invalid ({exc}). "
                "Respond with ONLY valid JSON matching the schema, no other text."
            )
            continue

        deduped: list[CuratorPick] = []
        seen_ids: set[int] = set()
        for pick in sorted(output.picks, key=lambda p: p.rank):
            if pick.cluster_id not in valid_ids or pick.cluster_id in seen_ids:
                continue
            seen_ids.add(pick.cluster_id)
            deduped.append(pick)
        return deduped[:pick_count]

    raise RuntimeError(f"Curator output failed schema validation twice: {last_error}")


def _select_diverse(
    picks: list[CuratorPick], candidates_by_id: dict[int, dict], max_per_country: int, max_per_topic: int
) -> list[CuratorPick]:
    """Greedily selects 10 from the LLM's ranked (oversampled) picks,
    enforcing hard per-day caps on country and topic tags. Picks skipped for
    exceeding a cap are deferred, not dropped: if the caps leave fewer than
    10 selected once the ranked list is exhausted, remaining slots are
    backfilled from the deferred picks (highest-rank first) ignoring the
    caps, so a lopsided news day still ships a full top 10 rather than
    coming up short."""
    selected: list[CuratorPick] = []
    deferred: list[CuratorPick] = []
    country_counts: dict[str, int] = {}
    topic_counts: dict[str, int] = {}

    for pick in picks:
        if len(selected) >= 10:
            break
        cand = candidates_by_id.get(pick.cluster_id)
        if cand is None:
            continue
        country = cand.get("country_tag")
        topic = cand.get("topic_tag")
        country_ok = not country or country_counts.get(country, 0) < max_per_country
        topic_ok = not topic or topic_counts.get(topic, 0) < max_per_topic
        if country_ok and topic_ok:
            selected.append(pick)
            if country:
                country_counts[country] = country_counts.get(country, 0) + 1
            if topic:
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
        else:
            deferred.append(pick)

    for pick in deferred:
        if len(selected) >= 10:
            break
        selected.append(pick)

    return selected[:10]


def process_all() -> dict:
    cfg = get_config()
    curate_cfg = cfg["curate"]
    model = cfg["llm"]["model"]
    api_key = get_api_key()
    candidate_window_hours = curate_cfg.get("candidate_window_hours", DEFAULT_CANDIDATE_WINDOW_HOURS)
    min_candidates = curate_cfg.get("min_candidates", DEFAULT_MIN_CANDIDATES)
    max_candidates = curate_cfg.get("max_candidates", 40)
    oversample_count = curate_cfg.get("oversample_count", DEFAULT_OVERSAMPLE_COUNT)
    max_per_country = curate_cfg.get("max_per_country", DEFAULT_MAX_PER_COUNTRY)
    max_per_topic = curate_cfg.get("max_per_topic", DEFAULT_MAX_PER_TOPIC)
    repeat_lookback_days = curate_cfg.get("repeat_lookback_days", DEFAULT_REPEAT_LOOKBACK_DAYS)
    theme_priority = _load_theme_priority()

    with get_connection() as conn:
        # Thin-day fallback: a 24h window can return too few candidates for
        # max_per_country/max_per_topic to mean anything -- with a small pool
        # the backfill loop in _select_diverse fires on nearly every pick,
        # silently ignoring the diversity caps while still looking "curated".
        # Widening to 48h only when needed keeps the common case tightly
        # scoped to the last day, and logging it lets us see how often 24h
        # actually falls short.
        candidates, widened = _load_candidates_with_fallback(
            conn, candidate_window_hours, min_candidates, max_candidates
        )
        if widened:
            print(
                f"Fewer than min_candidates={min_candidates} in the "
                f"{candidate_window_hours}h window; widened to "
                f"{WIDENED_CANDIDATE_WINDOW_HOURS}h ({len(candidates)} candidate(s))."
            )
        recent_ids = _recent_top10_cluster_ids(conn, repeat_lookback_days)

    stats = {"candidates": len(candidates), "picked": 0}
    if not candidates:
        return stats

    for c in candidates:
        c["topic_tag"] = dominant_topic(c.get("topics"), theme_priority)
        c["country_tag"] = dominant_country(c.get("country"))
    candidates_by_id = {c["cluster_id"]: c for c in candidates}

    pick_count = min(oversample_count, len(candidates))
    prompt = build_prompt(candidates, pick_count, recent_ids)
    valid_ids = set(candidates_by_id)

    with httpx.Client() as client:
        ranked = _request_curation(client, api_key, model, prompt, valid_ids, pick_count)

    selected = _select_diverse(ranked, candidates_by_id, max_per_country, max_per_topic)

    today = utc_today().isoformat()
    with get_connection() as conn:
        conn.execute("DELETE FROM daily_top10 WHERE date = ?", (today,))
        for rank, pick in enumerate(selected, start=1):
            conn.execute(
                "INSERT INTO daily_top10 (date, rank, cluster_id, rationale) VALUES (?, ?, ?, ?)",
                (today, rank, pick.cluster_id, pick.rationale),
            )
    stats["picked"] = len(selected)
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
