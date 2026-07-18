"""Human confirmation queue for proposed prediction verdicts (brief feature #8).

GitHub Pages (the brief's locked-in hosting choice, Sec 2) serves static
files only -- there is no server to accept a "confirm" click from a browser,
so a literal one-click web review page can't write back to the database.
This is the practical adaptation: `python -m pipeline.review` is a local,
one-keypress-per-item confirmation loop over every prediction
pipeline.resolve proposed a verdict for (status='pending_review'). Nothing
in predictions.html can move a prediction to 'resolved' -- that page is
read-only, deliberately, and points back to this command (see
templates/predictions.html's review-hint banner).

Usage: python -m pipeline.review
"""

from __future__ import annotations

import json
import sys

from pipeline.db import get_connection, init_db


def _load_queue(conn) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM predictions WHERE status = 'pending_review' ORDER BY horizon_date ASC"
        )
    ]


def _print_item(p: dict) -> None:
    evidence = json.loads(p["verdict_evidence"]) if p["verdict_evidence"] else {"reasoning": "", "articles": []}
    print()
    print(f"--- Prediction #{p['id']} ---")
    print(f"Predictor : {p['predictor'] or 'unknown'}")
    print(f"Claim     : {p['claim']}")
    print(f"Metric    : {p['metric'] or 'unspecified'} ({p['direction'] or 'unspecified'})")
    print(f"Horizon   : {p['horizon_date']}")
    print(f"Proposed verdict: {p['verdict'].upper()}")
    print(f"LLM reasoning   : {evidence.get('reasoning', '')}")
    if evidence.get("articles"):
        print("Supporting articles:")
        for a in evidence["articles"]:
            print(f"  - {a['title']} ({a['url']})")
    else:
        print("Supporting articles: none cited")


def run() -> dict:
    init_db()
    stats = {"confirmed": 0, "rejected": 0, "expired": 0, "skipped": 0}

    with get_connection() as conn:
        queue = _load_queue(conn)

    if not queue:
        print("Nothing awaiting review.")
        return stats

    print(f"{len(queue)} prediction(s) awaiting review.")
    for p in queue:
        _print_item(p)
        while True:
            choice = input("[y]confirm  [n]reject (recheck later)  [x]give up (expire)  [s]skip  [q]uit: ").strip().lower()
            if choice in ("y", "n", "x", "s", "q"):
                break
            print("  please enter y, n, x, s, or q")

        if choice == "q":
            break
        with get_connection() as conn:
            if choice == "y":
                conn.execute("UPDATE predictions SET status = 'resolved' WHERE id = ?", (p["id"],))
                stats["confirmed"] += 1
            elif choice == "n":
                conn.execute(
                    "UPDATE predictions SET status = 'open', verdict = NULL, verdict_evidence = NULL WHERE id = ?",
                    (p["id"],),
                )
                stats["rejected"] += 1
            elif choice == "x":
                conn.execute(
                    "UPDATE predictions SET status = 'expired', verdict = NULL, verdict_evidence = NULL WHERE id = ?",
                    (p["id"],),
                )
                stats["expired"] += 1
            else:
                stats["skipped"] += 1

    return stats


def main() -> dict:
    stats = run()
    print(
        f"\nDone: {stats['confirmed']} confirmed, {stats['rejected']} rejected, "
        f"{stats['expired']} expired, {stats['skipped']} skipped."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
