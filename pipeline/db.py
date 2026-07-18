"""SQLite schema and connection helper for the news aggregator.

Design goals:
 - Single-file db (data/aggregator.db), safe to open/close every CLI
   invocation -- no long-lived connection or background process.
 - WAL journal mode so a crash or laptop sleep mid-write doesn't corrupt
   the file and readers (e.g. an interrupted `build`) aren't blocked by
   writers (`fetch`/`process`).
 - `articles.processed_status` is a state machine (see PROCESSED_STATUSES
   below). Every pipeline stage filters on its expected input status and
   only advances rows it successfully finished, so re-running a stage after
   a crash just picks up wherever it left off instead of redoing work or
   skipping it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pipeline.config import get_config, resolve_path

# Valid values for articles.processed_status, in pipeline order.
# 'error' is a sink status any stage can move a row into on failure, so it
# stops being retried every run until a human/later phase looks at it.
PROCESSED_STATUSES = ("fetched", "extracted", "summarised", "done", "error")

SCHEMA = """
-- One row per RSS/Atom feed the pipeline polls.
CREATE TABLE IF NOT EXISTS feeds (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    country             TEXT,
    language            TEXT,
    topic_hint          TEXT,
    active              INTEGER NOT NULL DEFAULT 1,   -- 0/1: set to 0 for dead feeds instead of deleting
    consecutive_failures INTEGER NOT NULL DEFAULT 0,  -- reset to 0 on any successful fetch
    last_success        TEXT,                         -- ISO8601 timestamp, NULL until first success
    etag                TEXT,                         -- ETag from the last fetch response, for conditional GETs
    last_modified       TEXT,                         -- Last-Modified from the last fetch response, for conditional GETs
    last_error_type     TEXT                          -- classification of the most recent failure (http_404, timeout, dns, ssl, connection, other); NULL after a success
);

-- One row per article. url_hash (sha256 of the canonical URL) is the PK so
-- inserts are naturally idempotent -- re-fetching the same feed item is a
-- no-op rather than a duplicate row.
CREATE TABLE IF NOT EXISTS articles (
    url_hash        TEXT PRIMARY KEY,
    feed_id         INTEGER NOT NULL REFERENCES feeds(id),
    title           TEXT,
    original_title  TEXT,           -- title before translation, if translated
    url             TEXT NOT NULL,
    published       TEXT,           -- ISO8601, from feed metadata (may be NULL/unreliable)
    fetched         TEXT NOT NULL,  -- ISO8601, when our pipeline first saw it
    raw_text        TEXT,           -- extracted article body (trafilatura fallback if feed content is thin); English after translation
    original_raw_text TEXT,         -- raw_text before translation, if translated
    summary         TEXT,
    language        TEXT,
    country         TEXT,
    topics          TEXT,           -- comma-separated theme names matched from config/taxonomy.yaml; '' if tagged with no match, NULL if not yet tagged
    score           INTEGER,        -- count of matched taxonomy keywords (aggregator.py:489-522 score_entry); used to rank cluster members, NOT the daily top 10 (that's LLM-curated, see daily_top10)
    cluster_id      INTEGER REFERENCES clusters(id),
    processed_status TEXT NOT NULL DEFAULT 'fetched'
                    CHECK (processed_status IN ('fetched','extracted','summarised','done','error'))
);

-- Groups of articles covering the same story, assigned during a later
-- clustering phase (title-word/TF-IDF similarity -- see brief Phase 3).
CREATE TABLE IF NOT EXISTS clusters (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created                 TEXT NOT NULL,   -- ISO8601
    label                   TEXT,            -- short human-readable topic label
    representative_article  TEXT REFERENCES articles(url_hash)
);

-- Forecast/claim tracking: a prediction made by some source about an
-- economic metric, logged so its outcome can be checked later.
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    predictor       TEXT,            -- who/what made the claim (person, institution, model)
    source          TEXT,            -- article url_hash or external reference
    claim           TEXT NOT NULL,
    metric          TEXT,            -- e.g. "US CPI YoY", "ECB deposit rate"
    direction       TEXT,            -- e.g. "up", "down", "unchanged"
    horizon_date    TEXT,            -- ISO8601 date the prediction is about
    logged_date     TEXT NOT NULL,   -- ISO8601, when we recorded the claim
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','expired')),
    verdict         TEXT,            -- filled in once resolved: e.g. "correct", "incorrect"
    verdict_evidence TEXT
);

-- LLM-curated daily top 10 (brief feature #3 -- replaces v1's keyword-count
-- sort). Scoped by date rather than a column on clusters because the same
-- cluster's significance ranking can differ day to day, and this pipeline
-- re-curates several times through the day as more news comes in: each
-- pipeline.curate run for "today" deletes and re-inserts today's rows.
CREATE TABLE IF NOT EXISTS daily_top10 (
    date            TEXT NOT NULL,   -- ISO8601 date (UTC) this ranking is for
    rank            INTEGER NOT NULL,
    cluster_id      INTEGER NOT NULL REFERENCES clusters(id),
    rationale       TEXT,            -- LLM's one-sentence reason for inclusion/rank
    PRIMARY KEY (date, rank)
);

-- Audit log of every pipeline invocation, so `run.py all` history and
-- resume/debugging don't depend on external log files.
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    command         TEXT NOT NULL,   -- validate/fetch/process/build/publish/all
    started         TEXT NOT NULL,   -- ISO8601
    finished        TEXT,            -- ISO8601, NULL while still running or if it crashed
    status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','success','failed')),
    articles_in     INTEGER DEFAULT 0,
    articles_out    INTEGER DEFAULT 0,
    error_message   TEXT,
    detail          TEXT             -- free-text stage-specific summary (e.g. feeds tried/ok/failed)
);

-- Lookup indexes for the queries each stage runs repeatedly.
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(processed_status);
CREATE INDEX IF NOT EXISTS idx_articles_feed ON articles(feed_id);
CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(cluster_id);
CREATE INDEX IF NOT EXISTS idx_feeds_active ON feeds(active);
"""


def get_db_path() -> Path:
    cfg = get_config()
    return resolve_path(cfg["paths"]["db_file"])


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Yields a sqlite3 connection with WAL mode + foreign keys enabled.

    Use as a context manager so the connection is always closed:
        with get_connection() as conn:
            conn.execute(...)
    """
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Creates all tables/indexes if they don't already exist. Safe to call every run."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        existing_feed_cols = {row["name"] for row in conn.execute("PRAGMA table_info(feeds)")}
        for col, ddl in (("etag", "TEXT"), ("last_modified", "TEXT"), ("last_error_type", "TEXT")):
            if col not in existing_feed_cols:
                conn.execute(f"ALTER TABLE feeds ADD COLUMN {col} {ddl}")
        existing_run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "detail" not in existing_run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN detail TEXT")
        existing_article_cols = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
        if "original_raw_text" not in existing_article_cols:
            conn.execute("ALTER TABLE articles ADD COLUMN original_raw_text TEXT")
        if "score" not in existing_article_cols:
            conn.execute("ALTER TABLE articles ADD COLUMN score INTEGER")


if __name__ == "__main__":
    # `python -m pipeline.db` initialises the db file in place -- handy for
    # a first-time setup check without going through run.py.
    init_db()
    print(f"Initialised schema at {get_db_path()}")
