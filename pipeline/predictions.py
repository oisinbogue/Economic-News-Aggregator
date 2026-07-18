"""Prediction extraction pass (brief feature #8, "Extraction").

For each article with processed_status IN ('summarised', 'done') that hasn't
been checked yet (articles.prediction_checked = 0):
  1. Ask Cerebras whether the article contains an explicit, falsifiable
     prediction -- who/what made the claim, which metric or event, which
     direction, and a concrete resolution date. This is a second LLM pass
     over already-summarised text, run as its own module (rather than
     inline in pipeline.summarize) so it has its own per-run budget and
     can't become the long pole in an incremental Actions run.
  2. Only log predictions with a real, parseable horizon date -- a vague
     hedge ("inflation may ease at some point") gets no HORIZON the model
     can commit to, so it's discarded rather than logged as unresolvable
     from day one.
  3. Every checked article is marked prediction_checked = 1 regardless of
     outcome (found, none found, or an API error) so a bad article can't
     burn budget being retried every run -- same "sink" philosophy as
     pipeline.summarize's processed_status = 'error'.

Resolution (checking whether a logged prediction came true) is a separate
pass -- see pipeline.resolve -- since it only applies once horizon_date has
passed and needs a different prompt (archive search + verdict, not
extraction).

Usage: python -m pipeline.predictions
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone

import httpx

from pipeline.cerebras import call as cerebras_call, get_api_key, load_dotenv
from pipeline.config import get_config
from pipeline.db import get_connection, init_db

PREDICTION_FIELDS = ("PREDICTOR", "CLAIM", "METRIC", "DIRECTION", "HORIZON")


def build_prompt(title: str, body: str) -> str:
    return (
        "Does the following news article contain an explicit, falsifiable "
        "prediction or forecast about a specific economic metric or event -- "
        "one with a stated direction and a concrete resolution date? A vague "
        "hedge like \"inflation may ease at some point\" does NOT count; it "
        "must be specific enough to later check as right or wrong.\n\n"
        "If it does, respond in exactly this format and no other text:\n"
        "PREDICTOR: <who or what made the claim -- person, institution, or model>\n"
        "CLAIM: <one sentence stating the prediction>\n"
        "METRIC: <the metric or event, e.g. \"US CPI YoY\", \"ECB deposit rate\">\n"
        "DIRECTION: <up / down / unchanged / a short phrase>\n"
        "HORIZON: <the resolution date as YYYY-MM-DD. The article rarely states an "
        "exact day -- convert whatever time reference it gives into a specific date: "
        "a bare year like \"2027\" becomes 2027-12-31, a quarter like \"Q4 2027\" "
        "becomes 2027-12-31, \"by mid-2027\" becomes 2027-06-30, \"next month\" or "
        "\"next year\" is relative to this article's own date. Do not answer NONE "
        "just because the date isn't given to the day.>\n\n"
        "If there is no such prediction, respond with exactly: NONE\n\n"
        f"Title: {title}\n\n"
        f"Article text:\n{(body or '')[:1500]}"
    )


def parse_prediction(raw: str) -> dict | None:
    """Returns a dict with lowercase field names, or None if the model found
    nothing, didn't follow the format, or gave a horizon that isn't a real
    parseable date (treated the same as a discarded vague hedge)."""
    if raw.strip().upper().startswith("NONE"):
        return None

    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        key = key.strip().upper()
        if sep and key in PREDICTION_FIELDS:
            fields[key] = value.strip()

    if "CLAIM" not in fields or "HORIZON" not in fields:
        return None

    try:
        horizon = date.fromisoformat(fields["HORIZON"][:10])
    except ValueError:
        return None

    return {
        "predictor": fields.get("PREDICTOR") or None,
        "claim": fields["CLAIM"],
        "metric": fields.get("METRIC") or None,
        "direction": fields.get("DIRECTION") or None,
        "horizon_date": horizon.isoformat(),
    }


def process_all(limit: int | None = None) -> dict:
    cfg = get_config()
    model = cfg["llm"]["model"]
    api_key = get_api_key()
    if limit is None:
        limit = cfg["run"].get("max_predictions_per_run", 15)

    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT url_hash, title, summary
                FROM articles
                WHERE processed_status IN ('summarised', 'done')
                  AND prediction_checked = 0
                ORDER BY fetched
                LIMIT ?
                """,
                (limit,),
            )
        ]

    stats = {"checked": 0, "found": 0, "errors": 0}
    if not rows:
        return stats

    with httpx.Client() as client:
        for row in rows:
            try:
                prompt = build_prompt(row["title"], row["summary"])
                raw = cerebras_call(client, api_key, model, prompt, max_tokens=900)
                prediction = parse_prediction(raw)
            except Exception as exc:
                prediction = None
                stats["errors"] += 1
                print(f"  [error] {row['url_hash'][:12]}: {exc}", file=sys.stderr)

            with get_connection() as conn:
                if prediction:
                    conn.execute(
                        """
                        INSERT INTO predictions
                            (predictor, source, claim, metric, direction,
                             horizon_date, logged_date, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
                        """,
                        (
                            prediction["predictor"], row["url_hash"], prediction["claim"],
                            prediction["metric"], prediction["direction"], prediction["horizon_date"],
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    stats["found"] += 1
                conn.execute(
                    "UPDATE articles SET prediction_checked = 1 WHERE url_hash = ?",
                    (row["url_hash"],),
                )
            stats["checked"] += 1

    return stats


def main() -> dict:
    load_dotenv()
    init_db()
    print("Extracting falsifiable predictions from summarised articles...")
    stats = process_all()
    print(
        f"Done: {stats['checked']} article(s) checked, {stats['found']} prediction(s) logged, "
        f"{stats['errors']} error(s)."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
