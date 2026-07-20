"""Prediction resolution pass (brief feature #8, "Resolution").

For each logged prediction whose horizon_date has passed and is still
status='open':
  1. Search the archive (brief feature #7 -- this is exactly the "everything
     tagged X in the last N days" query the corpus exists for) for articles
     published on/after the horizon date that plausibly cover the outcome,
     using the prediction's metric/claim significant words.
  2. If nothing plausible has shown up yet, leave it 'open' and try again on
     a later run -- coverage of an outcome doesn't always land the same day
     -- unless `predictions.resolution_grace_days` has elapsed since the
     horizon, in which case it's marked 'expired' with no verdict rather
     than checked forever.
  3. If candidates exist, ask Cerebras to propose a verdict (correct /
     incorrect / unresolvable) citing which candidate articles support it.
     This is only ever a *proposal*: status becomes 'pending_review', never
     'resolved' -- brief feature #8 explicitly says not to auto-publish an
     unverified verdict. A human confirms or rejects it via pipeline.review.

Usage: python -m pipeline.resolve
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

from pipeline.cerebras import call as cerebras_call, get_api_key, load_dotenv
from pipeline.cluster import _sig_words
from pipeline.config import get_config
from pipeline.db import get_connection, init_db
from pipeline.gh_issues import create_issue

VERDICT_RE = re.compile(r"VERDICT:\s*(correct|incorrect|unresolvable)", re.IGNORECASE)
EVIDENCE_RE = re.compile(r"EVIDENCE:\s*(.+)")
REASONING_RE = re.compile(r"REASONING:\s*(.+)")

VALID_VERDICTS = {"correct", "incorrect", "unresolvable"}


def _search_words(prediction: dict) -> list[str]:
    """Significant words to search the archive for coverage of this
    prediction's outcome -- metric is more specific than the full claim
    sentence, so prefer it, falling back to the claim if metric is missing."""
    text = prediction.get("metric") or prediction["claim"]
    words = sorted(_sig_words(text))
    return words[:6]


def find_candidates(conn, prediction: dict, max_candidates: int) -> list[dict]:
    words = _search_words(prediction)
    if not words:
        return []

    clauses = " OR ".join(["(title LIKE ? OR summary LIKE ?)"] * len(words))
    params: list[str] = []
    for w in words:
        like = f"%{w}%"
        params.extend([like, like])
    params.extend([prediction["horizon_date"], max_candidates])

    rows = conn.execute(
        f"""
        SELECT url_hash, title, summary, url, feed_id, fetched
        FROM articles
        WHERE ({clauses}) AND fetched >= ?
        ORDER BY fetched ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def gather_context_articles(conn, prediction: dict, days: int, limit: int) -> list[dict]:
    """Recent articles sharing the source article's country/topics (brief
    feature #8 extension) -- background context for the LLM beyond the
    keyword-matched outcome candidates in find_candidates, since a prediction
    made about e.g. "US CPI" can be usefully informed by other US/inflation
    coverage even if it doesn't directly report the outcome."""
    source_hash = prediction.get("source")
    if not source_hash:
        return []

    source_row = conn.execute(
        "SELECT country, topics FROM articles WHERE url_hash = ?", (source_hash,)
    ).fetchone()
    if not source_row:
        return []

    country = source_row["country"]
    topics = [t.strip() for t in (source_row["topics"] or "").split(",") if t.strip()]
    if not country and not topics:
        return []

    clauses = []
    params: list[str] = []
    if country:
        clauses.append("country = ?")
        params.append(country)
    for topic in topics:
        clauses.append("topics LIKE ?")
        params.append(f"%{topic}%")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    params.extend([cutoff, source_hash, limit])

    rows = conn.execute(
        f"""
        SELECT url_hash, title, summary, url, fetched
        FROM articles
        WHERE ({" OR ".join(clauses)}) AND fetched >= ? AND url_hash != ?
        ORDER BY fetched DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def build_prompt(prediction: dict, candidates: list[dict], context_articles: list[dict] | None = None) -> str:
    listing = "\n".join(
        f"[{i}] {c['title']} -- {(c['summary'] or '')[:200]}"
        for i, c in enumerate(candidates)
    )
    context_section = ""
    if context_articles:
        context_listing = "\n".join(
            f"- {c['title']} -- {(c['summary'] or '')[:200]}" for c in context_articles
        )
        context_section = (
            "\nAdditional recent coverage of the same country/topic, for background "
            f"context only (not indexed for citation):\n{context_listing}\n"
        )
    return (
        "A prediction was logged from a news article. Decide, using ONLY the "
        "candidate articles below (which may or may not actually cover the "
        "outcome), whether the prediction turned out correct, incorrect, or "
        "unresolvable from the available coverage.\n\n"
        f"Predictor: {prediction.get('predictor') or 'unknown'}\n"
        f"Claim: {prediction['claim']}\n"
        f"Metric: {prediction.get('metric') or 'unspecified'}\n"
        f"Predicted direction: {prediction.get('direction') or 'unspecified'}\n"
        f"Resolution horizon: {prediction['horizon_date']}\n"
        f"{context_section}\n"
        f"Candidate articles (bracketed index):\n{listing}\n\n"
        "Respond in exactly this format and no other text:\n"
        "VERDICT: <correct|incorrect|unresolvable>\n"
        "EVIDENCE: <comma-separated bracketed indices of the article(s) that support your verdict, or NONE>\n"
        "REASONING: <one concise sentence>"
    )


def parse_verdict(raw: str, num_candidates: int) -> dict | None:
    v_match = VERDICT_RE.search(raw)
    if not v_match:
        return None
    verdict = v_match.group(1).lower()

    e_match = EVIDENCE_RE.search(raw)
    evidence_idx: list[int] = []
    if e_match and e_match.group(1).strip().upper() != "NONE":
        for tok in re.findall(r"\d+", e_match.group(1)):
            idx = int(tok)
            if 0 <= idx < num_candidates:
                evidence_idx.append(idx)

    r_match = REASONING_RE.search(raw)
    reasoning = r_match.group(1).strip() if r_match else ""

    return {"verdict": verdict, "evidence_idx": evidence_idx, "reasoning": reasoning}


def resolve_one(client: httpx.Client, api_key: str, model: str, conn, prediction: dict, max_candidates: int) -> str:
    """Returns what happened: 'proposed', 'expired', 'skipped', or 'no_candidates'."""
    candidates = find_candidates(conn, prediction, max_candidates)

    if not candidates:
        horizon = date.fromisoformat(prediction["horizon_date"])
        grace_days = get_config()["predictions"].get("resolution_grace_days", 21)
        if (date.today() - horizon).days > grace_days:
            conn.execute(
                "UPDATE predictions SET status = 'expired' WHERE id = ?", (prediction["id"],)
            )
            return "expired"
        return "no_candidates"

    cfg = get_config()["predictions"]
    context_articles = gather_context_articles(
        conn, prediction,
        days=cfg.get("context_window_days", 14),
        limit=cfg.get("context_articles_limit", 8),
    )

    prompt = build_prompt(prediction, candidates, context_articles)
    raw = cerebras_call(client, api_key, model, prompt, max_tokens=900)
    parsed = parse_verdict(raw, len(candidates))
    if not parsed or parsed["verdict"] not in VALID_VERDICTS:
        return "skipped"

    evidence_articles = [
        {
            "title": candidates[i]["title"],
            "url": candidates[i]["url"],
            "url_hash": candidates[i]["url_hash"],
        }
        for i in parsed["evidence_idx"]
    ]
    evidence = {"reasoning": parsed["reasoning"], "articles": evidence_articles}
    conn.execute(
        "UPDATE predictions SET status = 'pending_review', verdict = ?, verdict_evidence = ? WHERE id = ?",
        (parsed["verdict"], json.dumps(evidence, ensure_ascii=False), prediction["id"]),
    )

    # Best-effort: opening the review issue is a convenience, not the source
    # of truth (the DB row is) -- a failure here shouldn't fail the whole
    # resolve pass, since pipeline.review still works as a fallback.
    try:
        create_issue(prediction, parsed["verdict"], parsed["reasoning"], evidence_articles, context_articles)
    except Exception as exc:
        print(f"  [warn] could not open review issue for prediction #{prediction['id']}: {exc}", file=sys.stderr)

    return "proposed"


def process_all(limit: int | None = None) -> dict:
    cfg = get_config()
    model = cfg["llm"]["model"]
    api_key = get_api_key()
    max_candidates = cfg["predictions"].get("max_candidate_articles", 15)
    if limit is None:
        limit = cfg["run"].get("max_predictions_per_run", 15)

    today = date.today().isoformat()
    with get_connection() as conn:
        due = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM predictions
                WHERE status = 'open' AND horizon_date <= ?
                ORDER BY horizon_date ASC
                LIMIT ?
                """,
                (today, limit),
            )
        ]

    stats = {"checked": 0, "proposed": 0, "expired": 0, "still_open": 0, "errors": 0}
    if not due:
        return stats

    with httpx.Client() as client:
        for prediction in due:
            try:
                with get_connection() as conn:
                    outcome = resolve_one(client, api_key, model, conn, prediction, max_candidates)
            except Exception as exc:
                outcome = "errors"
                print(f"  [error] prediction #{prediction['id']}: {exc}", file=sys.stderr)

            stats["checked"] += 1
            if outcome == "proposed":
                stats["proposed"] += 1
            elif outcome == "expired":
                stats["expired"] += 1
            elif outcome == "errors":
                stats["errors"] += 1
            else:
                stats["still_open"] += 1

    return stats


def main() -> dict:
    load_dotenv()
    init_db()
    print("Resolving predictions past their horizon...")
    stats = process_all()
    print(
        f"Done: {stats['checked']} checked, {stats['proposed']} proposed for review, "
        f"{stats['expired']} expired (no coverage found in time), {stats['still_open']} still waiting, "
        f"{stats['errors']} error(s)."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
