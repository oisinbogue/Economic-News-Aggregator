"""Pre-upload backup verification, plus the size/count visibility surfaces
that sit on top of it (job summary, status.json, GitHub Issue alert).

The durable backup (.github/workflows/pipeline.yml "Back up article archive
to GitHub Releases") uploads a uniquely-named snapshot every run and never
overwrites one in place -- but that guarantee only protects against a partial
*upload*, not against faithfully uploading a *corrupt* db (e.g. a crash mid
-write that WAL didn't fully recover, or a pruning bug that silently deletes
most of a table). This module runs before that upload: a PRAGMA
integrity_check plus a row-count comparison against the previous snapshot's
counts (stored alongside it as a same-named `aggregator.counts.*.json`
asset -- same "one per run, never overwritten" scheme as the db itself).

Also reused, unmodified, by .github/workflows/backup-restore-test.yml: that
workflow downloads the newest snapshot onto a fresh runner and runs the same
integrity_check to prove the backup is actually restorable, not just
uploaded.

On failure this module doesn't just exit non-zero: it also opens/reuses a
GitHub Issue (the same alert channel already wired up for prediction
approvals, see pipeline/gh_issues.py) so a bad backup surfaces somewhere a
human will see it even if nobody is watching the Actions tab.

Usage:
    python -m pipeline.backup_verify --repo owner/name --write-counts data/backup_counts.json
    python -m pipeline.backup_verify --repo owner/name --no-alert   # restore-test workflow
    python -m pipeline.backup_verify --write-status-json site/status.json \
        --last-snapshot-run-id 12345 --last-snapshot-at 2026-07-21T06:00:00Z
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import get_config
from pipeline.db import get_connection, get_db_path
from pipeline.gh_issues import gh_available

# Tables worth tracking for backup-health purposes. Deliberately excludes
# article_embeddings/prediction_embeddings -- those are large, re-derivable
# from articles/predictions by pipeline.embed, and would just add noise to
# the drop-detection below (they get rebuilt in batches, not steadily).
TABLES = (
    "feeds", "articles", "clusters", "predictions", "daily_top10",
    "resolution_audit",
)

# A table needs at least this many rows in the previous snapshot before a
# drop is worth flagging -- otherwise normal small-number noise (e.g.
# daily_top10 going from 3 rows to 0 between curate runs) would trigger
# false alarms.
MIN_ROWS_FOR_DROP_CHECK = 5

ALERT_LABEL = "backup-integrity-failure"

COUNTS_ASSET_PREFIX = "aggregator.counts."


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
    """Downloads and parses the newest `aggregator.counts.*.json` release
    asset. Returns None (never raises) if `gh` isn't available, there's no
    prior snapshot (first run ever), or anything about the lookup fails --
    a missing baseline means "nothing to compare against yet", not a
    verification failure."""
    if not gh_available():
        return None
    try:
        proc = subprocess.run(
            [
                "gh", "api", f"repos/{repo}/releases/tags/db-latest", "--jq",
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
                    "gh", "release", "download", "db-latest", "--repo", repo,
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
    snapshot. An implausible drop is more likely a pruning bug or a bad
    restore than real data loss -- this pipeline never deletes articles in
    bulk except cleanup_live_blogs (a one-off purge, small vs. the archive)."""
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
    lines = ["## Backup verification", ""]
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
    Issue so a failed verification surfaces even if nobody's watching the
    Actions tab. Degrades to a no-op if `gh` isn't available, same as
    pipeline.gh_issues -- CI already fails loudly via this step's exit code
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
        "Automated backup verification failed -- see run for details.",
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
            ["gh", "issue", "create", "--title", "Backup verification failed",
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


def write_status_json(path: Path, last_snapshot_run_id: str | None, last_snapshot_at: str | None) -> dict:
    """Writes the small status.json the site's source-health dashboard can
    read (db size, per-table row counts, last-successful-snapshot time) --
    same data as the job summary, just machine-readable and published
    alongside the static site."""
    with get_connection() as conn:
        counts = table_counts(conn)
    status = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "db_size_bytes": db_size_bytes(),
        "counts": counts,
        "last_snapshot_run_id": last_snapshot_run_id,
        "last_snapshot_at": last_snapshot_at,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="owner/name, for comparing against the previous snapshot's counts")
    parser.add_argument("--max-drop-fraction", type=float, default=None)
    parser.add_argument("--write-counts", type=Path, help="write current counts+size to this JSON path")
    parser.add_argument("--no-alert", action="store_true", help="skip filing a GitHub Issue on failure")
    parser.add_argument("--write-status-json", type=Path, help="write site status.json instead of verifying")
    parser.add_argument("--last-snapshot-run-id", help="with --write-status-json")
    parser.add_argument("--last-snapshot-at", help="with --write-status-json")
    args = parser.parse_args()

    if args.write_status_json:
        status = write_status_json(args.write_status_json, args.last_snapshot_run_id, args.last_snapshot_at)
        print(f"Wrote {args.write_status_json}: {status['db_size_bytes'] / 1_048_576:.1f} MiB, "
              f"{sum(status['counts'].values())} total rows across tracked tables.")
        sys.stdout.flush()
        return 0

    cfg = get_config()
    max_drop_fraction = args.max_drop_fraction
    if max_drop_fraction is None:
        max_drop_fraction = cfg.get("backup", {}).get("max_row_count_drop_fraction", 0.5)

    result = verify(args.repo, max_drop_fraction)
    write_job_summary(result)

    if not result["ok"]:
        print(f"Backup verification FAILED: {'; '.join(result['issues'])}", file=sys.stderr)
        if not args.no_alert:
            file_alert_issue(result)
        return 1

    if args.write_counts:
        write_counts_file(result, args.write_counts)

    print("Backup verification passed.")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
