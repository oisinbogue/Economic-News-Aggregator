"""GitHub Issues plumbing for the prediction-resolution approval workflow
(brief feature #8 extension).

pipeline.resolve proposes a verdict but must never publish it unconfirmed.
Previously the only confirmation path was a local keypress (pipeline.review)
-- this module lets the same proposal be reviewed from a GitHub Issue instead
(approve/reject labels handled by .github/workflows/resolve-approval.yml),
so the human step no longer requires being at the laptop.

Every issue this module creates carries a hidden HTML comment recording the
prediction's row id -- `PREDICTION_ID_MARKER.format(id)` -- which is the one
and only place prediction-id parsing is defined. pipeline.apply_resolution
(the approval side) imports `extract_prediction_id` from here rather than
re-deriving its own regex, so the two sides of the workflow can't drift.

Every function here degrades to a no-op (returns None / False) if `gh` isn't
on PATH or isn't authenticated -- this keeps local development working
without a GitHub token, same as before this feature existed.

Usage: from pipeline.gh_issues import create_issue, find_open_issue
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

PREDICTION_ID_MARKER = "<!-- prediction-id: {id} -->"
PREDICTION_ID_RE = re.compile(r"<!--\s*prediction-id:\s*(\d+)\s*-->")

# DB verdict values ('correct'/'incorrect'/'unresolvable') vs. the label
# vocabulary requested for issues (verdict-correct/verdict-wrong/verdict-unresolvable).
VERDICT_LABELS = {
    "correct": "verdict-correct",
    "incorrect": "verdict-wrong",
    "unresolvable": "verdict-unresolvable",
}

PENDING_LABEL = "pending-resolution"


def gh_available() -> bool:
    """True if the gh CLI is installed and authenticated. Best-effort check --
    an unauthenticated `gh` still exists on PATH, so a quick `gh auth status`
    call is needed rather than just shutil.which."""
    if not shutil.which("gh"):
        return False
    try:
        subprocess.run(
            ["gh", "auth", "status"], capture_output=True, timeout=10, check=True
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def extract_prediction_id(body: str) -> int | None:
    match = PREDICTION_ID_RE.search(body or "")
    return int(match.group(1)) if match else None


_LABEL_COLORS = {
    PENDING_LABEL: "fbca04",
    "verdict-correct": "0e8a16",
    "verdict-wrong": "d93f0b",
    "verdict-unresolvable": "c5def5",
    "rejected": "e11d48",
}


def _ensure_labels(names: list[str]) -> None:
    """Best-effort: `gh issue create --label X` fails outright if label X
    doesn't exist in the repo yet, so create any missing ones first. Ignores
    "already exists" failures -- this is just making sure the label exists,
    not tracking whether this call was the one that created it."""
    for name in names:
        try:
            subprocess.run(
                ["gh", "label", "create", name, "--color", _LABEL_COLORS.get(name, "ededed"), "--force"],
                capture_output=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass


def find_open_issue(prediction_id: int) -> int | None:
    """Returns the open issue number already tracking this prediction, if
    any, so resolve.py doesn't open a duplicate on a rerun before a human
    has acted on the first one."""
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "list",
                "--label", PENDING_LABEL,
                "--state", "open",
                "--json", "number,body",
                "--limit", "200",
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None

    try:
        issues = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    for issue in issues:
        if extract_prediction_id(issue.get("body", "")) == prediction_id:
            return issue["number"]
    return None


def _build_body(
    prediction: dict,
    verdict: str,
    reasoning: str,
    evidence_articles: list[dict],
    context_articles: list[dict],
) -> str:
    lines = [
        f"**Claim:** {prediction['claim']}",
        f"**Predictor:** {prediction.get('predictor') or 'unknown'}",
        f"**Metric:** {prediction.get('metric') or 'unspecified'}",
        f"**Predicted direction:** {prediction.get('direction') or 'unspecified'}",
        f"**Resolution horizon:** {prediction.get('horizon_date') or 'unspecified'}",
        "",
        f"**Proposed verdict:** {verdict.upper()}",
        f"**LLM reasoning:** {reasoning}",
        "",
    ]

    if evidence_articles:
        lines.append("**Cited evidence:**")
        lines.extend(f"- [{a['title']}]({a['url']})" for a in evidence_articles)
    else:
        lines.append("**Cited evidence:** none cited")

    if context_articles:
        lines.append("")
        lines.append("**Additional recent coverage considered (same country/topic, last 14 days):**")
        lines.extend(f"- [{a['title']}]({a['url']})" for a in context_articles)

    lines.extend([
        "",
        "---",
        "To act on this from anywhere: add label `approve` to confirm the verdict "
        "above as-is, or `reject` to send the prediction back to `open` for a later "
        "recheck (no verdict is written). To confirm a *different* verdict than the "
        "one proposed, comment `verdict: correct`, `verdict: incorrect`, or "
        "`verdict: unresolvable` before adding `approve` -- the workflow uses your "
        "comment instead of the LLM's proposal.",
        "",
        PREDICTION_ID_MARKER.format(id=prediction["id"]),
    ])
    return "\n".join(lines)


def create_issue(
    prediction: dict,
    verdict: str,
    reasoning: str,
    evidence_articles: list[dict],
    context_articles: list[dict],
) -> int | None:
    """Opens a GitHub Issue for human review of a proposed verdict. Returns
    the issue number, or None if one already exists for this prediction, or
    if gh isn't available (caller should treat this as best-effort)."""
    if not gh_available():
        return None

    if find_open_issue(prediction["id"]) is not None:
        return None

    short_claim = prediction["claim"][:80]
    if len(prediction["claim"]) > 80:
        short_claim = short_claim.rsplit(" ", 1)[0] + "..."
    title = f"[Resolve] {short_claim} — proposed: {verdict}"
    body = _build_body(prediction, verdict, reasoning, evidence_articles, context_articles)

    verdict_label = VERDICT_LABELS.get(verdict)
    labels = [PENDING_LABEL] + ([verdict_label] if verdict_label else [])
    _ensure_labels(labels)

    args = ["gh", "issue", "create", "--title", title, "--body", body]
    for label in labels:
        args.extend(["--label", label])

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None

    # `gh issue create` prints the created issue's URL on success.
    match = re.search(r"/issues/(\d+)", proc.stdout)
    return int(match.group(1)) if match else None
