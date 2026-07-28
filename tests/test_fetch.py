import asyncio
import sqlite3

import pytest

import pipeline.fetch as fetch_mod
from pipeline.db import SCHEMA


# --- normalise_url / hash_url -------------------------------------------------

def test_normalise_url_strips_tracking_params_and_fragment():
    url = "https://example.com/story/?utm_source=rss&fbclid=abc&id=1#top"
    assert fetch_mod.normalise_url(url) == "https://example.com/story?id=1"


def test_normalise_url_strips_trailing_slash():
    assert fetch_mod.normalise_url("https://example.com/story/") == "https://example.com/story"


def test_hash_url_is_deterministic_sha256():
    url = "https://example.com/story"
    expected = fetch_mod.hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert fetch_mod.hash_url(url) == expected


# --- db_writer -----------------------------------------------------------------

def _make_db(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO feeds (id, url, name) VALUES (1, 'https://feed.example/rss', 'Feed')"
    )
    conn.commit()
    conn.close()
    return path


def test_db_writer_inserts_items_and_commits_each_one(tmp_path):
    db_path = _make_db(tmp_path)

    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        writer = asyncio.create_task(fetch_mod.db_writer(queue, db_path))

        await queue.put(("hash1", 1, "Title 1", "https://example.com/1", None, "2026-01-01T00:00:00+00:00", "body1", None))
        # First item should be committed and visible before we enqueue the second.
        await queue.join()
        conn = sqlite3.connect(db_path)
        rows_after_first = conn.execute("SELECT url_hash FROM articles").fetchall()
        conn.close()

        await queue.put(("hash2", 1, "Title 2", "https://example.com/2", None, "2026-01-01T00:00:01+00:00", "body2", None))
        await queue.join()

        await queue.put(None)
        await writer
        return rows_after_first

    rows_after_first = asyncio.run(run())
    assert rows_after_first == [("hash1",)]

    conn = sqlite3.connect(db_path)
    final_hashes = {row[0] for row in conn.execute("SELECT url_hash FROM articles")}
    conn.close()
    assert final_hashes == {"hash1", "hash2"}


def test_db_writer_on_conflict_do_nothing_keeps_first_row(tmp_path):
    db_path = _make_db(tmp_path)

    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        writer = asyncio.create_task(fetch_mod.db_writer(queue, db_path))
        await queue.put(("dupe", 1, "Original", "https://example.com/o", None, "2026-01-01T00:00:00+00:00", "body", None))
        await queue.put(("dupe", 1, "Duplicate", "https://example.com/d", None, "2026-01-01T00:00:01+00:00", "body", None))
        await queue.put(None)
        await writer

    asyncio.run(run())

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT title FROM articles WHERE url_hash = 'dupe'").fetchone()
    conn.close()
    assert row[0] == "Original"


# --- process_feed dedup logic ---------------------------------------------------

class _FakeSemaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _entry(link, title="Title", body="x" * 400):
    return {"link": link, "title": title, "summary": body}


class _FakeParsed:
    def __init__(self, entries):
        self.entries = entries


def _feed_row(feed_id=1, active=1):
    return {
        "id": feed_id, "url": "https://feed.example/rss", "name": "Feed",
        "etag": None, "last_modified": None, "active": active,
    }


async def _run_process_feed(monkeypatch, entries, existing_hashes,
                             canonical_url=None, canonical_page_body="",
                             articles_remaining=None, max_articles_per_feed=20):
    async def fake_fetch_feed(client, feed, timeout):
        return True, _FakeParsed(entries), {}

    async def fake_extract_article_page(client, url, timeout):
        return canonical_page_body or None, None, canonical_url

    monkeypatch.setattr(fetch_mod, "fetch_feed", fake_fetch_feed)
    monkeypatch.setattr(fetch_mod, "extract_article_page", fake_extract_article_page)
    monkeypatch.setattr(fetch_mod, "get_connection", lambda: _NullConn())

    stats = fetch_mod.RunStats()
    write_queue: asyncio.Queue = asyncio.Queue()
    await fetch_mod.process_feed(
        client=None,
        semaphore=_FakeSemaphore(),
        feed=_feed_row(),
        fetch_timeout=5,
        stats=stats,
        stats_lock=asyncio.Lock(),
        articles_remaining=articles_remaining if articles_remaining is not None else [100],
        articles_remaining_lock=asyncio.Lock(),
        max_articles_per_feed=max_articles_per_feed,
        existing_hashes=existing_hashes,
        write_queue=write_queue,
    )
    queued = []
    while not write_queue.empty():
        queued.append(write_queue.get_nowait())
    return stats, queued


class _NullConn:
    """Stands in for get_connection() around the feed-health UPDATE writes,
    which process_feed still issues directly (out of scope for this
    optimisation) -- process_feed tests here only care about article dedup.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return self


def test_process_feed_skips_url_already_in_existing_hashes(monkeypatch):
    url = "https://example.com/story"
    existing_hashes = {fetch_mod.hash_url(fetch_mod.normalise_url(url))}

    stats, queued = asyncio.run(
        _run_process_feed(monkeypatch, [_entry(url)], existing_hashes)
    )

    assert queued == []
    assert stats.new_articles == 0


def test_process_feed_queues_new_article_and_reserves_hash(monkeypatch):
    url = "https://example.com/story"
    existing_hashes: set[str] = set()

    stats, queued = asyncio.run(
        _run_process_feed(monkeypatch, [_entry(url)], existing_hashes)
    )

    expected_hash = fetch_mod.hash_url(fetch_mod.normalise_url(url))
    assert len(queued) == 1
    assert queued[0][0] == expected_hash
    assert stats.new_articles == 1
    # Reserved in-memory so a duplicate entry later in the same run is skipped.
    assert expected_hash in existing_hashes


def test_process_feed_stops_at_per_feed_cap_leaving_global_budget_untouched(monkeypatch):
    entries = [_entry(f"https://example.com/story-{i}") for i in range(5)]
    remaining = [100]

    stats, queued = asyncio.run(
        _run_process_feed(
            monkeypatch, entries, set(),
            articles_remaining=remaining, max_articles_per_feed=2,
        )
    )

    assert len(queued) == 2
    assert stats.new_articles == 2
    assert remaining[0] == 98


def test_process_feed_canonical_url_dedup_skips_when_already_known(monkeypatch):
    url = "https://example.com/syndicated-copy"
    canonical_url = "https://example.com/original-story"
    canonical_hash = fetch_mod.hash_url(fetch_mod.normalise_url(canonical_url))
    existing_hashes = {canonical_hash}

    # Short body forces the extract_article_page() fallback, which is where
    # canonical-URL resolution happens.
    stats, queued = asyncio.run(
        _run_process_feed(
            monkeypatch, [_entry(url, body="short")], existing_hashes,
            canonical_url=canonical_url,
        )
    )

    assert queued == []
    assert stats.new_articles == 0


def test_process_feed_canonical_url_dedup_switches_hash_when_new(monkeypatch):
    url = "https://example.com/syndicated-copy"
    canonical_url = "https://example.com/original-story"
    existing_hashes: set[str] = set()

    stats, queued = asyncio.run(
        _run_process_feed(
            monkeypatch, [_entry(url, body="short")], existing_hashes,
            canonical_url=canonical_url, canonical_page_body="extracted body text",
        )
    )

    expected_hash = fetch_mod.hash_url(fetch_mod.normalise_url(canonical_url))
    assert len(queued) == 1
    assert queued[0][0] == expected_hash
    assert queued[0][3] == fetch_mod.normalise_url(canonical_url)
    assert stats.new_articles == 1
