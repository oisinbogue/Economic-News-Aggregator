import asyncio
import sqlite3
import ssl
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import pipeline.fetch as fetch_mod
from pipeline.db import SCHEMA


# --- normalise_url / hash_url -------------------------------------------------

def test_normalise_url_strips_tracking_params_and_fragment():
    url = "https://example.com/story/?utm_source=rss&fbclid=abc&id=1#top"
    assert fetch_mod.normalise_url(url) == "https://example.com/story?id=1"


def test_normalise_url_strips_trailing_slash():
    assert fetch_mod.normalise_url("https://example.com/story/") == "https://example.com/story"


@pytest.mark.parametrize("param", ["utm_medium", "utm_campaign", "ns_campaign", "icid", "mc_cid", "cmp"])
def test_normalise_url_strips_tracking_prefixes(param):
    url = f"https://example.com/story?{param}=x&id=1"
    assert fetch_mod.normalise_url(url) == "https://example.com/story?id=1"


@pytest.mark.parametrize("name", [
    "fbclid", "gclid", "msclkid", "mkt_tok", "ref", "refid", "cmpid",
    "spref", "share", "source", "smid", "ocid", "wt_mc", "utm",
])
def test_normalise_url_strips_tracking_names(name):
    url = f"https://example.com/story?{name}=x&id=1"
    assert fetch_mod.normalise_url(url) == "https://example.com/story?id=1"


def test_normalise_url_tracking_name_match_is_case_insensitive():
    url = "https://example.com/story?FBCLID=x&id=1"
    assert fetch_mod.normalise_url(url) == "https://example.com/story?id=1"


def test_normalise_url_no_query_params_left_drops_question_mark():
    url = "https://example.com/story?utm_source=rss&fbclid=abc"
    assert fetch_mod.normalise_url(url) == "https://example.com/story"


def test_normalise_url_root_path_becomes_slash():
    assert fetch_mod.normalise_url("https://example.com") == "https://example.com/"
    assert fetch_mod.normalise_url("https://example.com/") == "https://example.com/"


def test_normalise_url_strips_fragment_only():
    url = "https://example.com/story#section-2"
    assert fetch_mod.normalise_url(url) == "https://example.com/story"


def test_normalise_url_keeps_non_tracking_query_params_and_order():
    url = "https://example.com/story?b=2&a=1"
    assert fetch_mod.normalise_url(url) == "https://example.com/story?b=2&a=1"


def test_normalise_url_trailing_slash_and_tracking_and_fragment_combined():
    url = "https://example.com/story/?id=1&gclid=xyz#top"
    assert fetch_mod.normalise_url(url) == "https://example.com/story?id=1"


def test_hash_url_is_deterministic_sha256():
    url = "https://example.com/story"
    expected = fetch_mod.hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert fetch_mod.hash_url(url) == expected


# --- LIVE_BLOG_URL_RE ---------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.theguardian.com/politics/live/2026/jul/28/lords-peerages-live",
    "https://example.com/news/2026/07/28/economy-live-updates",
    "https://example.com/liveblog/budget-day",
    "https://example.com/live-blog/election-night",
    "https://example.com/2026/business-liveblog",
    "https://example.com/coverage/rolling-coverage/budget-2026",
])
def test_live_blog_regex_matches_known_shapes(url):
    assert fetch_mod.LIVE_BLOG_URL_RE.search(url)


@pytest.mark.parametrize("url", [
    "https://example.com/2026/07/28/fed-raises-rates",
    "https://example.com/news/deliveries",
    "https://example.com/story/how-we-live-our-lives",
    "https://example.com/olive/branch",
])
def test_live_blog_regex_does_not_match_ordinary_articles(url):
    assert fetch_mod.LIVE_BLOG_URL_RE.search(url) is None


# --- parse_published -----------------------------------------------------------

def _struct_for(dt: datetime) -> time.struct_time:
    # parse_published() feeds *_parsed structs through time.mktime(), which
    # interprets a struct_time as local time -- mirror that here (rather than
    # dt.timetuple(), which would silently assume local time == UTC) so the
    # round trip back through fromtimestamp(..., utc) lands on `dt` regardless
    # of the machine's timezone.
    return time.localtime(dt.timestamp())


def test_parse_published_uses_published_parsed_struct():
    dt = datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc)
    entry = {"published_parsed": _struct_for(dt)}
    result = fetch_mod.parse_published(entry)
    assert result.startswith("2026-07-20T12:30:00")


def test_parse_published_falls_back_to_updated_parsed():
    dt = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    entry = {"published_parsed": None, "updated_parsed": _struct_for(dt)}
    result = fetch_mod.parse_published(entry)
    assert result.startswith("2026-07-21T08:00:00")


def test_parse_published_falls_back_to_created_parsed():
    dt = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)
    entry = {"created_parsed": _struct_for(dt)}
    result = fetch_mod.parse_published(entry)
    assert result.startswith("2026-07-22T09:00:00")


def test_parse_published_falls_back_to_rfc822_published_string():
    entry = {"published": "Mon, 20 Jul 2026 12:30:00 GMT"}
    result = fetch_mod.parse_published(entry)
    assert result.startswith("2026-07-20T12:30:00")


def test_parse_published_falls_back_to_updated_string_when_published_string_bad():
    entry = {"published": "not a date", "updated": "Mon, 20 Jul 2026 12:30:00 GMT"}
    result = fetch_mod.parse_published(entry)
    assert result.startswith("2026-07-20T12:30:00")


def test_parse_published_returns_none_when_nothing_usable():
    assert fetch_mod.parse_published({}) is None
    assert fetch_mod.parse_published({"published": "garbage", "updated": "also garbage"}) is None


def test_parse_published_handles_overflow_struct_by_falling_through():
    # year 10000 overflows datetime -- struct_time with an absurd year.
    bad_struct = (10000, 1, 1, 0, 0, 0, 0, 1, 0)
    entry = {
        "published_parsed": bad_struct,
        "published": "Mon, 20 Jul 2026 12:30:00 GMT",
    }
    result = fetch_mod.parse_published(entry)
    assert result.startswith("2026-07-20T12:30:00")


# --- extract_entry_image precedence --------------------------------------------

def test_extract_entry_image_prefers_media_thumbnail():
    entry = {
        "media_thumbnail": [{"url": "https://example.com/thumb.jpg"}],
        "media_content": [{"url": "https://example.com/content.jpg", "medium": "image"}],
        "enclosures": [{"href": "https://example.com/enc.jpg", "type": "image/jpeg"}],
        "summary": '<img src="https://example.com/inline.jpg">',
    }
    assert fetch_mod.extract_entry_image(entry) == "https://example.com/thumb.jpg"


def test_extract_entry_image_falls_back_to_media_content_when_no_thumbnail():
    entry = {
        "media_content": [{"url": "https://example.com/content.jpg", "medium": "image"}],
        "enclosures": [{"href": "https://example.com/enc.jpg", "type": "image/jpeg"}],
        "summary": '<img src="https://example.com/inline.jpg">',
    }
    assert fetch_mod.extract_entry_image(entry) == "https://example.com/content.jpg"


def test_extract_entry_image_media_content_requires_image_medium_or_type():
    entry = {
        "media_content": [{"url": "https://example.com/video.mp4", "medium": "video"}],
        "enclosures": [{"href": "https://example.com/enc.jpg", "type": "image/jpeg"}],
    }
    assert fetch_mod.extract_entry_image(entry) == "https://example.com/enc.jpg"


def test_extract_entry_image_falls_back_to_enclosure_when_no_media_fields():
    entry = {
        "enclosures": [{"href": "https://example.com/enc.jpg", "type": "image/jpeg"}],
        "links": [{"rel": "enclosure", "type": "image/png", "href": "https://example.com/link.png"}],
        "summary": '<img src="https://example.com/inline.jpg">',
    }
    assert fetch_mod.extract_entry_image(entry) == "https://example.com/enc.jpg"


def test_extract_entry_image_falls_back_to_links_when_no_enclosure():
    entry = {
        "links": [{"rel": "enclosure", "type": "image/png", "href": "https://example.com/link.png"}],
        "summary": '<img src="https://example.com/inline.jpg">',
    }
    assert fetch_mod.extract_entry_image(entry) == "https://example.com/link.png"


def test_extract_entry_image_falls_back_to_img_in_summary_html_last():
    entry = {"summary": '<p>text</p><img src="https://example.com/inline.jpg" alt="x">'}
    assert fetch_mod.extract_entry_image(entry) == "https://example.com/inline.jpg"


def test_extract_entry_image_checks_content_before_summary_for_inline_img():
    entry = {
        "content": [{"value": '<img src="https://example.com/from-content.jpg">'}],
        "summary": '<img src="https://example.com/from-summary.jpg">',
    }
    assert fetch_mod.extract_entry_image(entry) == "https://example.com/from-content.jpg"


def test_extract_entry_image_returns_none_when_nothing_found():
    assert fetch_mod.extract_entry_image({"summary": "<p>no image here</p>"}) is None


# --- classify_fetch_error -------------------------------------------------------

def test_classify_fetch_error_http_status_includes_code():
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("not found", request=request, response=response)
    assert fetch_mod.classify_fetch_error(exc) == "http_404"


def test_classify_fetch_error_timeout():
    assert fetch_mod.classify_fetch_error(httpx.TimeoutException("timed out")) == "timeout"


def test_classify_fetch_error_dns():
    exc = httpx.ConnectError("[Errno -2] Name or service not known")
    assert fetch_mod.classify_fetch_error(exc) == "dns"


def test_classify_fetch_error_dns_name_resolution_error_variant():
    exc = httpx.ConnectError("NameResolutionError encountered")
    assert fetch_mod.classify_fetch_error(exc) == "dns"


def test_classify_fetch_error_ssl_from_message():
    exc = httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED")
    assert fetch_mod.classify_fetch_error(exc) == "ssl"


def test_classify_fetch_error_ssl_from_cause():
    exc = httpx.ConnectError("handshake failed")
    exc.__cause__ = ssl.SSLError("bad handshake")
    assert fetch_mod.classify_fetch_error(exc) == "ssl"


def test_classify_fetch_error_generic_connect_error_is_connection():
    exc = httpx.ConnectError("Connection refused")
    assert fetch_mod.classify_fetch_error(exc) == "connection"


def test_classify_fetch_error_other_for_unrecognised_exception():
    assert fetch_mod.classify_fetch_error(ValueError("weird")) == "other"


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

        await queue.put(("hash1", 1, "Title 1", "https://example.com/1", None, "2026-01-01T00:00:00+00:00", None, "body1", None))
        # First item should be committed and visible before we enqueue the second.
        await queue.join()
        conn = sqlite3.connect(db_path)
        rows_after_first = conn.execute("SELECT url_hash FROM articles").fetchall()
        conn.close()

        await queue.put(("hash2", 1, "Title 2", "https://example.com/2", None, "2026-01-01T00:00:01+00:00", None, "body2", None))
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
        await queue.put(("dupe", 1, "Original", "https://example.com/o", None, "2026-01-01T00:00:00+00:00", None, "body", None))
        await queue.put(("dupe", 1, "Duplicate", "https://example.com/d", None, "2026-01-01T00:00:01+00:00", None, "body", None))
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
        run_id=1,
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


# --- auto-recovery (feed deactivation / cooldown / reactivation) --------------

@pytest.fixture
def feeds_db(tmp_path, monkeypatch):
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

    monkeypatch.setattr(fetch_mod, "get_connection", get_connection)
    monkeypatch.setattr(fetch_mod, "get_db_path", lambda: path)
    return path


def _insert_feed(path, feed_id=1, url="https://feed.example/rss", active=1,
                  consecutive_failures=0, deactivated_reason=None, last_attempt=None):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO feeds (id, url, name, active, consecutive_failures, deactivated_reason, last_attempt)
        VALUES (?, ?, 'Feed', ?, ?, ?, ?)
        """,
        (feed_id, url, active, consecutive_failures, deactivated_reason, last_attempt),
    )
    conn.commit()
    conn.close()


def _get_feed_row(path, feed_id=1):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    conn.close()
    return dict(row)


async def _run_single_feed(feed_row):
    stats = fetch_mod.RunStats()
    write_queue: asyncio.Queue = asyncio.Queue()
    await fetch_mod.process_feed(
        client=None,
        semaphore=_FakeSemaphore(),
        feed=feed_row,
        fetch_timeout=5,
        stats=stats,
        stats_lock=asyncio.Lock(),
        articles_remaining=[100],
        articles_remaining_lock=asyncio.Lock(),
        max_articles_per_feed=20,
        existing_hashes=set(),
        write_queue=write_queue,
        run_id=1,
    )
    return stats


def test_process_feed_deactivates_at_max_consecutive_failures(monkeypatch, feeds_db):
    _insert_feed(feeds_db, active=1, consecutive_failures=fetch_mod.MAX_CONSECUTIVE_FAILURES - 1)
    feed_row = _get_feed_row(feeds_db)

    async def failing_fetch_feed(client, feed, timeout):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(fetch_mod, "fetch_feed", failing_fetch_feed)

    asyncio.run(_run_single_feed(feed_row))

    updated = _get_feed_row(feeds_db)
    assert updated["consecutive_failures"] == fetch_mod.MAX_CONSECUTIVE_FAILURES
    assert updated["active"] == 0
    assert updated["deactivated_reason"] == "auto_failure"
    assert updated["last_error_type"] == "connection"


def test_process_feed_failure_below_threshold_stays_active(monkeypatch, feeds_db):
    _insert_feed(feeds_db, active=1, consecutive_failures=0)
    feed_row = _get_feed_row(feeds_db)

    async def failing_fetch_feed(client, feed, timeout):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(fetch_mod, "fetch_feed", failing_fetch_feed)

    asyncio.run(_run_single_feed(feed_row))

    updated = _get_feed_row(feeds_db)
    assert updated["consecutive_failures"] == 1
    assert updated["active"] == 1
    assert updated["deactivated_reason"] is None


def test_process_feed_successful_probe_reactivates_feed(monkeypatch, feeds_db):
    _insert_feed(
        feeds_db, active=0, consecutive_failures=fetch_mod.MAX_CONSECUTIVE_FAILURES,
        deactivated_reason="auto_failure",
    )
    feed_row = _get_feed_row(feeds_db)

    async def ok_fetch_feed(client, feed, timeout):
        return False, None, {}  # 304 Not Modified -- still a success for feed health

    monkeypatch.setattr(fetch_mod, "fetch_feed", ok_fetch_feed)

    stats = asyncio.run(_run_single_feed(feed_row))

    updated = _get_feed_row(feeds_db)
    assert updated["active"] == 1
    assert updated["deactivated_reason"] is None
    assert updated["consecutive_failures"] == 0
    assert stats.feeds_recovered == 1


def test_fetch_all_cooldown_excludes_recently_probed_inactive_feed(monkeypatch, feeds_db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()  # inside a 24h cooldown
    old = (now - timedelta(hours=48)).isoformat()  # past a 24h cooldown

    _insert_feed(feeds_db, feed_id=1, url="https://feed.example/recent",
                 active=0, deactivated_reason="auto_failure", last_attempt=recent)
    _insert_feed(feeds_db, feed_id=2, url="https://feed.example/old",
                 active=0, deactivated_reason="auto_failure", last_attempt=old)
    _insert_feed(feeds_db, feed_id=3, url="https://feed.example/manual",
                 active=0, deactivated_reason="manual", last_attempt=old)

    probed_feed_ids = []

    async def fake_process_feed(client, semaphore, feed, *args, **kwargs):
        probed_feed_ids.append(feed["id"])

    monkeypatch.setattr(fetch_mod, "process_feed", fake_process_feed)

    asyncio.run(
        fetch_mod.fetch_all(
            concurrency=5, timeout=5, max_articles=10,
            recovery_interval_hours=24, recovery_probes_per_run=15,
            max_articles_per_feed=5,
        )
    )

    # Only the feed whose last_attempt is outside the cooldown window is
    # retried; the recently-probed one is skipped, and the manually-muted
    # one is never eligible for auto-recovery at all.
    assert probed_feed_ids == [2]
