"""Keeps data/aggregator.db from growing without bound now that articles
accumulate forever (nothing else in the pipeline deletes rows in bulk except
cleanup_live_blogs's one-off purge).

Two independent jobs, both safe to re-run:
 - prune_old_text: blanks raw_text/original_raw_text on articles old enough
   that nothing downstream still needs the full body -- summary, tags,
   cluster membership and prediction records are untouched. Gated on
   processed_status so a still-mid-pipeline row (raw_text not yet
   summarised) never loses the text summarize.py needs to produce that
   summary. Neither pipeline.resolve nor pipeline.predictions reads
   raw_text (both work off summary/title), so pruning it doesn't touch
   prediction-resolution quality even for long-horizon predictions.
 - vacuum: reclaims the space pruning (and normal deletes) frees. Run
   weekly rather than every invocation -- VACUUM rewrites the whole file,
   which is wasted work at 6 runs/day for a few KB of weekly churn, and
   briefly needs ~2x the db's disk space.

Usage: python -m pipeline.db_maintenance
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from pipeline.config import get_config
from pipeline.db import get_connection, get_db_path, init_db

DEFAULT_PRUNE_AFTER_DAYS = 90

# Weekly VACUUM window: Sunday, first cron slot only (pipeline.yml runs at
# 6,9,12,15,18,21 UTC) -- so a Sunday only VACUUMs once instead of at every
# one of that day's 6 runs. isoweekday() 7 == Sunday.
VACUUM_WEEKDAY = 7
VACUUM_HOUR_RANGE = (6, 9)


def prune_old_text(conn, days: int = DEFAULT_PRUNE_AFTER_DAYS) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur = conn.execute(
        """
        UPDATE articles
        SET raw_text = NULL, original_raw_text = NULL
        WHERE fetched < ?
          AND processed_status IN ('summarised', 'done', 'error')
          AND (raw_text IS NOT NULL OR original_raw_text IS NOT NULL)
        """,
        (cutoff,),
    )
    return {"pruned": cur.rowcount}


def should_vacuum(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now.isoweekday() == VACUUM_WEEKDAY and VACUUM_HOUR_RANGE[0] <= now.hour < VACUUM_HOUR_RANGE[1]


def vacuum() -> None:
    """Runs outside pipeline.db's get_connection wrapper -- VACUUM can't run
    inside a transaction and rewrites the whole file, so it gets its own
    plain connection rather than sharing the WAL/foreign-keys setup used for
    everything else."""
    conn = sqlite3.connect(get_db_path())
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def db_size_bytes() -> int:
    path = get_db_path()
    return path.stat().st_size if path.exists() else 0


def write_job_summary(before_bytes: int, after_bytes: int, prune_stats: dict, vacuumed: bool) -> None:
    lines = [
        "## DB maintenance",
        "",
        f"**Size before:** {before_bytes / 1_048_576:.1f} MiB",
        f"**Size after:** {after_bytes / 1_048_576:.1f} MiB",
        f"**Articles pruned (raw text cleared):** {prune_stats['pruned']}",
        f"**VACUUM ran:** {'yes' if vacuumed else 'no (not due this run)'}",
        "",
    ]
    text = "\n".join(lines)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)


def main() -> dict:
    init_db()
    cfg = get_config()
    prune_after_days = cfg.get("maintenance", {}).get("prune_text_after_days", DEFAULT_PRUNE_AFTER_DAYS)

    before_bytes = db_size_bytes()

    with get_connection() as conn:
        prune_stats = prune_old_text(conn, prune_after_days)

    vacuumed = should_vacuum()
    if vacuumed:
        vacuum()

    after_bytes = db_size_bytes()

    print(
        f"Pruned raw text on {prune_stats['pruned']} article(s) older than {prune_after_days}d. "
        f"Size {before_bytes / 1_048_576:.1f} MiB -> {after_bytes / 1_048_576:.1f} MiB "
        f"({'VACUUMed' if vacuumed else 'no VACUUM this run'})."
    )
    write_job_summary(before_bytes, after_bytes, prune_stats, vacuumed)
    sys.stdout.flush()
    return {**prune_stats, "size_before_bytes": before_bytes, "size_after_bytes": after_bytes, "vacuumed": vacuumed}


if __name__ == "__main__":
    main()
