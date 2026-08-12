"""Pre-upload backup verification for the indicators subsystem.

Twin of `pipeline/backup_verify.py`, adapted for `data/indicators.db` and
its own release tag (`indicators-db-latest`, distinct from the news
pipeline's `db-latest`) -- see .github/workflows/indicators.yml's "Back up
indicators database to GitHub Releases" step, which this module gates the
same way pipeline.backup_verify gates pipeline.yml's upload.

Deliberately self-contained rather than importing anything from `pipeline/`
(same reason `indicators/config.py` doesn't import `pipeline/config.py`:
the indicators subsystem is a separate failure domain from the news
pipeline, INDICATORS_BRIEF.md §2). The GitHub Issues helpers below are a
small, intentional duplication of `pipeline/gh_issues.py`'s
`gh_available()` for that reason -- pulling in the real module would wire
the two failure domains together through their alerting path.

Also reused, unmodified, by .github/workflows/backup-restore-test.yml's
indicators job: downloads the newest indicators-db-latest snapshot onto a
fresh runner and runs the same integrity_check to prove the backup is
actually restorable, not just uploaded.

Usage:
    python -m indicators.backup_verify --repo owner/name --write-counts data/indicators_backup_counts.json
    python -m indicators.backup_verify --repo owner/name --no-alert   # restore-test workflow
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from indicators.config import get_config
from indicators.db import get_connection, get_db_path

# Tables worth tracking for backup-health purposes. Deliberately excludes
# fetch_log -- it's an append-only per-run log, not a data table, and its
# row count grows unboundedly rather than reflecting real data loss the way
# a drop in `series`/`observations` would.
TABLES = ("series", "observations", "observation_revisions", "releases", "fx_rates")

# A table needs at least this many rows in the previous snapshot before a
# drop is worth flagging -- otherwise normal small-number noise would
# trigger false alarms (e.g. a five-country Phase-1 db has few `releases`
# rows to begin with).
MIN_ROWS_FOR_DROP_CHECK = 5

RELEASE_TAG = "indicators-db-latest"
ALERT_LABEL = "indicators-backup-integrity-failure"
COUNTS_ASSET_PREFIX = "indicators.counts."


# --------------------------------------------------------------------------
# gh CLI helpers (duplicated from pipeline/gh_issues.py -- see module
# docstring for why this isn't a shared import)
# --------------------------------------------------------------------------

def gh_available() -> bool:
    if not shutil.which("gh"):
        return False
    try:
        subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=10, check=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def integrity_check(conn) -> str:
    """Returns 'ok', or the first reported problem if PRAGMA integrity_check
    finds one (it can return many rows; the first is enough to fail loudly)."""
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if len(rows) == 1 and rows[0][0] == "ok":
        return "ok"
    return rows[0][0] if rows else "integrity_check returned no rows"


def table_counts(conn) -> dict[str, int]:
    counts = {}
    for table in TABLES:
        counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return counts


def db_size_bytes() -> int:
    path = get_db_path()
    return path.stat().st_size if path.exists() else 0


def fetch_previous_counts(repo: str) -> dict | None:
    """Downloads and parses the newest `indicators.counts.*.json` release
    asset from `indicators-db-latest`. Returns None (never raises) if `gh`
    isn't available, there's no prior snapshot (first run ever), or
    anything about the lookup fails -- a missing baseline means "nothing to
    compare against yet", not a verification failure."""
    if not gh_available():
        return None
    try:
        proc = subprocess.run(
            [
                "gh", "api", f"repos/{repo}/releases/tags/{RELEASE_TAG}", "--jq",
                f'[.assets[] | select(.name | startswith("{COUNTS_ASSET_PREFIX}"))] '
                "| sort_by(.created_at) | last | .name",
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        asset_name = proc.stdout.strip().strip('"')
        if not asset_name or asset_name == "null":
            return None

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / asset_name
            subprocess.run(
                [
                    "gh", "release", "download", RELEASE_TAG, "--repo", repo,
                    "--pattern", asset_name, "--output", str(out_path), "--clobber",
                ],
                capture_output=True, timeout=60, check=True,
            )
            data = json.loads(out_path.read_text(encoding="utf-8"))
            return data.get("counts")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None


def compare_counts(
    previous: dict[str, int] | None, current: dict[str, int], max_drop_fraction: float
) -> list[str]:
    """Flags tables whose row count dropped implausibly vs. the previous
    snapshot -- more likely a bad restore or a config/fetch bug than real
    data loss, since the indicators subsystem never bulk-deletes rows."""
    if not previous:
        return []
    issues = []
    for table in TABLES:
        prev_n = previous.get(table, 0)
        cur_n = current.get(table, 0)
        if prev_n < MIN_ROWS_FOR_DROP_CHECK:
            continue
        if cur_n < prev_n * (1 - max_drop_fraction):
            pct = 100 * (1 - cur_n / prev_n) if prev_n else 100
            issues.append(f"{table}: {prev_n} -> {cur_n} rows ({pct:.0f}% drop)")
    return issues


def verify(repo: str | None, max_drop_fraction: float) -> dict:
    with get_connection() as conn:
        integrity = integrity_check(conn)
        counts = table_counts(conn)

    previous_counts = fetch_previous_counts(repo) if repo else None
    count_issues = compare_counts(previous_counts, counts, max_drop_fraction)

    issues = count_issues[:]
    if integrity != "ok":
        issues.insert(0, f"integrity_check failed: {integrity}")

    return {
        "ok": not issues,
        "integrity": integrity,
        "counts": counts,
        "previous_counts": previous_counts,
        "issues": issues,
        "db_size_bytes": db_size_bytes(),
    }


def write_job_summary(result: dict) -> None:
    lines = ["## Indicators backup verification", ""]
    lines.append(f"**Result:** {'PASS' if result['ok'] else 'FAIL'}")
    lines.append(f"**Integrity check:** {result['integrity']}")
    lines.append(f"**DB size:** {result['db_size_bytes'] / 1_048_576:.1f} MiB")
    lines.append("")
    lines.append("| table | rows | previous |")
    lines.append("|---|---|---|")
    for table in TABLES:
        prev = result["previous_counts"].get(table) if result["previous_counts"] else "n/a"
        lines.append(f"| {table} | {result['counts'][table]} | {prev} |")
    if result["issues"]:
        lines.append("")
        lines.append("**Issues:**")
        lines.extend(f"- {issue}" for issue in result["issues"])
    text = "\n".join(lines) + "\n"

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)


def file_alert_issue(result: dict) -> int | None:
    """Best-effort: opens (or leaves alone, if one's already open) a GitHub
    Issue under its own label so an indicators backup failure surfaces
    separately from a news-pipeline one. Degrades to a no-op if `gh` isn't
    available -- CI already fails loudly via this step's exit code
    regardless of whether the issue could be filed."""
    if not gh_available():
        return None
    try:
        proc = subprocess.run(
            ["gh", "issue", "list", "--label", ALERT_LABEL, "--state", "open",
             "--json", "number", "--limit", "1"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        existing = json.loads(proc.stdout)
        if existing:
            return existing[0]["number"]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None

    body_lines = [
        "Automated indicators backup verification failed -- see run for details.",
        "",
        f"Integrity check: {result['integrity']}",
        "",
    ]
    if result["issues"]:
        body_lines.append("Issues:")
        body_lines.extend(f"- {issue}" for issue in result["issues"])

    try:
        subprocess.run(
            ["gh", "label", "create", ALERT_LABEL, "--color", "d93f0b", "--force"],
            capture_output=True, timeout=15,
        )
        proc = subprocess.run(
            ["gh", "issue", "create", "--title", "Indicators backup verification failed",
             "--body", "\n".join(body_lines), "--label", ALERT_LABEL],
            capture_output=True, text=True, timeout=30, check=True,
        )
        match = re.search(r"/issues/(\d+)", proc.stdout)
        return int(match.group(1)) if match else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def write_counts_file(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated": datetime.now(timezone.utc).isoformat(),
                "db_size_bytes": result["db_size_bytes"],
                "counts": result["counts"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="owner/name, for comparing against the previous snapshot's counts")
    parser.add_argument("--max-drop-fraction", type=float, default=None)
    parser.add_argument("--write-counts", type=Path, help="write current counts+size to this JSON path")
    parser.add_argument("--no-alert", action="store_true", help="skip filing a GitHub Issue on failure")
    args = parser.parse_args()

    cfg = get_config()
    max_drop_fraction = args.max_drop_fraction
    if max_drop_fraction is None:
        max_drop_fraction = cfg.get("backup", {}).get("max_row_count_drop_fraction", 0.5)

    result = verify(args.repo, max_drop_fraction)
    write_job_summary(result)

    if not result["ok"]:
        print(f"Indicators backup verification FAILED: {'; '.join(result['issues'])}", file=sys.stderr)
        if not args.no_alert:
            file_alert_issue(result)
        return 1

    if args.write_counts:
        write_counts_file(result, args.write_counts)

    print("Indicators backup verification passed.")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
