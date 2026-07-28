import sqlite3
from contextlib import contextmanager

import pytest

import pipeline.reconcile_feeds as reconcile_mod
from pipeline.db import SCHEMA


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    @contextmanager
    def get_connection():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    monkeypatch.setattr(reconcile_mod, "get_connection", get_connection)
    return path


def _insert_feed(path, url, name="Feed", active=1, deactivated_reason=None,
                  consecutive_failures=0):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO feeds (url, name, active, deactivated_reason, consecutive_failures)
        VALUES (?, ?, ?, ?, ?)
        """,
        (url, name, active, deactivated_reason, consecutive_failures),
    )
    conn.commit()
    conn.close()


def _feed_row(path, url):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _yaml_feed(url, name="Feed", active=True, country="US", language="en", topic_hint=""):
    return {"url": url, "name": name, "active": active, "country": country,
            "language": language, "topic_hint": topic_hint}


# --- load_feeds_yaml ------------------------------------------------------

def test_load_feeds_yaml_flattens_regions(tmp_path):
    yaml_path = tmp_path / "feeds.yaml"
    yaml_path.write_text(
        """
regions:
  us:
    - url: https://a.example/rss
      name: A
  eu:
    - url: https://b.example/rss
      name: B
""",
        encoding="utf-8",
    )
    feeds = reconcile_mod.load_feeds_yaml(yaml_path)
    assert {f["url"] for f in feeds} == {"https://a.example/rss", "https://b.example/rss"}


# --- reconcile: insert ------------------------------------------------------

def test_reconcile_inserts_new_active_feed(db):
    stats = reconcile_mod.reconcile([_yaml_feed("https://new.example/rss", active=True)])

    assert stats.inserted == 1
    row = _feed_row(db, "https://new.example/rss")
    assert row["active"] == 1
    assert row["deactivated_reason"] is None


def test_reconcile_inserts_new_inactive_feed_with_manual_reason(db):
    stats = reconcile_mod.reconcile([_yaml_feed("https://muted.example/rss", active=False)])

    assert stats.inserted == 1
    row = _feed_row(db, "https://muted.example/rss")
    assert row["active"] == 0
    assert row["deactivated_reason"] == "manual"


# --- reconcile: update existing, YAML active:true --------------------------

def test_reconcile_updates_metadata_without_touching_health_of_healthy_feed(db):
    _insert_feed(db, "https://existing.example/rss", name="Old Name", active=1,
                 deactivated_reason=None, consecutive_failures=3)

    stats = reconcile_mod.reconcile(
        [_yaml_feed("https://existing.example/rss", name="New Name", active=True)]
    )

    assert stats.updated == 1
    row = _feed_row(db, "https://existing.example/rss")
    assert row["name"] == "New Name"
    # active/failure-streak ownership stays with pipeline.fetch, not touched here.
    assert row["active"] == 1
    assert row["consecutive_failures"] == 3


def test_reconcile_leaves_auto_deactivated_feed_alone_when_still_active_in_yaml(db):
    _insert_feed(db, "https://flaky.example/rss", active=0,
                 deactivated_reason="auto_failure", consecutive_failures=5)

    reconcile_mod.reconcile([_yaml_feed("https://flaky.example/rss", active=True)])

    row = _feed_row(db, "https://flaky.example/rss")
    # A human never muted this one -- reconcile must not touch fetch's call.
    assert row["active"] == 0
    assert row["deactivated_reason"] == "auto_failure"
    assert row["consecutive_failures"] == 5


def test_reconcile_reactivates_manually_muted_feed_when_yaml_flips_back_to_active(db):
    _insert_feed(db, "https://unmuted.example/rss", active=0, deactivated_reason="manual")

    reconcile_mod.reconcile([_yaml_feed("https://unmuted.example/rss", active=True)])

    row = _feed_row(db, "https://unmuted.example/rss")
    assert row["active"] == 1
    assert row["deactivated_reason"] is None


# --- reconcile: update existing, YAML active:false --------------------------

def test_reconcile_active_false_in_yaml_forces_deactivation_of_healthy_feed(db):
    _insert_feed(db, "https://tobemuted.example/rss", active=1, deactivated_reason=None)

    reconcile_mod.reconcile([_yaml_feed("https://tobemuted.example/rss", active=False)])

    row = _feed_row(db, "https://tobemuted.example/rss")
    assert row["active"] == 0
    assert row["deactivated_reason"] == "manual"


# --- reconcile: removed from YAML -------------------------------------------

def test_reconcile_deactivates_feed_removed_from_yaml(db):
    _insert_feed(db, "https://gone.example/rss", active=1, deactivated_reason=None)

    stats = reconcile_mod.reconcile([_yaml_feed("https://kept.example/rss")])

    assert stats.deactivated == 1
    row = _feed_row(db, "https://gone.example/rss")
    assert row["active"] == 0
    assert row["deactivated_reason"] == "manual"
    # Never deleted -- fetch history/foreign keys from articles must survive.
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM feeds WHERE url = 'https://gone.example/rss'").fetchone()[0] == 1
    conn.close()


def test_reconcile_does_not_recount_already_inactive_feed_removed_from_yaml(db):
    _insert_feed(db, "https://already-gone.example/rss", active=0, deactivated_reason="auto_failure")

    stats = reconcile_mod.reconcile([_yaml_feed("https://kept.example/rss")])

    assert stats.deactivated == 0
    row = _feed_row(db, "https://already-gone.example/rss")
    # Not present in YAML at all, but reconcile shouldn't stomp the existing
    # auto_failure reason with 'manual' since it never re-touches rows
    # already inactive.
    assert row["deactivated_reason"] == "auto_failure"


# --- reconcile: duplicate URL within YAML -----------------------------------

def test_reconcile_duplicate_url_in_yaml_last_one_wins(db):
    stats = reconcile_mod.reconcile([
        _yaml_feed("https://dupe.example/rss", name="First"),
        _yaml_feed("https://dupe.example/rss", name="Second"),
    ])

    assert stats.inserted == 1
    row = _feed_row(db, "https://dupe.example/rss")
    assert row["name"] == "Second"


# --- reconcile: blank URL rows are skipped ----------------------------------

def test_reconcile_skips_rows_with_no_url(db):
    stats = reconcile_mod.reconcile([{"name": "No URL", "active": True}])

    assert stats.inserted == 0
    assert stats.updated == 0
    assert stats.deactivated == 0
