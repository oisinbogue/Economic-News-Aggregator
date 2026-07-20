"""Applies a human decision (from a GitHub Issue) to a proposed prediction
verdict (brief feature #8 extension -- see pipeline/gh_issues.py).

This is the one place that parses a prediction id back out of an issue body
and decides what to write to the db -- .github/workflows/resolve-approval.yml
calls this for both the `approve` and `reject` labels rather than duplicating
that logic in shell. The proposed verdict itself is read from the db (the
source of truth, written by pipeline.resolve), not re-parsed from the issue
body -- the issue only needs to carry a possible human *override*, via a
comment matching "verdict: <correct|incorrect|unresolvable>".

Prints a single JSON object to stdout so the calling workflow step can act on
the result (close the issue, swap labels) without re-deriving anything:
    {"ok": true, "prediction_id": 42, "issue_number": 7,
     "human_decision": "approved", "final_verdict": "correct"}
    {"ok": false, "error": "..."}

Usage: python -m pipeline.apply_resolution --issue-number 7 --action approve
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

from pipeline.db import get_connection, init_db
from pipeline.gh_issues import extract_prediction_id

OVERRIDE_RE = re.compile(r"verdict:\s*(correct|incorrect|unresolvable)", re.IGNORECASE)
VALID_VERDICTS = {"correct", "incorrect", "unresolvable"}


def _fetch_issue(issue_number: int) -> dict:
    proc = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "body,comments"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout)


def _find_override(comments: list[dict]) -> str | None:
    """Last comment matching the override pattern wins, so a correction
    posted after an earlier (mistaken) one still takes effect."""
    override = None
    for comment in comments:
        match = OVERRIDE_RE.search(comment.get("body", ""))
        if match:
            override = match.group(1).lower()
    return override


def apply(issue_number: int, action: str) -> dict:
    issue = _fetch_issue(issue_number)
    prediction_id = extract_prediction_id(issue.get("body", ""))
    if prediction_id is None:
        return {"ok": False, "error": f"no prediction-id marker found in issue #{issue_number} body"}

    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM predictions WHERE id = ?", (prediction_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": f"prediction #{prediction_id} not found in db"}
        prediction = dict(row)

        proposed_verdict = prediction.get("verdict")
        evidence = json.loads(prediction["verdict_evidence"]) if prediction.get("verdict_evidence") else {}
        proposed_reasoning = evidence.get("reasoning", "")

        if action == "approve":
            override = _find_override(issue.get("comments", []))
            if override and override in VALID_VERDICTS:
                final_verdict = override
                human_decision = "corrected"
            else:
                final_verdict = proposed_verdict
                human_decision = "approved"
            conn.execute(
                "UPDATE predictions SET status = 'resolved', verdict = ? WHERE id = ?",
                (final_verdict, prediction_id),
            )
        elif action == "reject":
            final_verdict = None
            human_decision = "rejected"
            conn.execute(
                "UPDATE predictions SET status = 'open', verdict = NULL, verdict_evidence = NULL WHERE id = ?",
                (prediction_id,),
            )
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}

        conn.execute(
            """
            INSERT INTO resolution_audit
                (prediction_id, proposed_verdict, proposed_reasoning, issue_number, human_decision, final_verdict, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction_id, proposed_verdict, proposed_reasoning, issue_number,
                human_decision, final_verdict, datetime.now(timezone.utc).isoformat(),
            ),
        )

    return {
        "ok": True,
        "prediction_id": prediction_id,
        "issue_number": issue_number,
        "human_decision": human_decision,
        "final_verdict": final_verdict,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--action", choices=["approve", "reject"], required=True)
    args = parser.parse_args()

    try:
        result = apply(args.issue_number, args.action)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    print(json.dumps(result))
    sys.stdout.flush()
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
