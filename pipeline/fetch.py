"""Fetches new articles from every active feed and inserts them into the db.

For each active feed:
  1. GET the feed URL concurrently (bounded by run.fetch_concurrency), sending
     If-None-Match / If-Modified-Since from the feed's stored etag/last_modified
     so unchanged feeds cost the remote server (and us) almost nothing.
  2. Parse with feedparser. For each entry, normalise its URL (strip tracking
     params) and sha256-hash it as the article's primary key -- if that hash
     is already in the db the entry is skipped without even hitting the network
     again.
  3. New entries get title/url/published/feed_id/summary from the feed itself.
     If the feed didn't supply enough body text, fall back to fetching the
     article page and extracting with trafilatura. Extraction failures still
     keep the article (title-only) rather than dropping it -- a thin article
     is more useful than a missing one, and process/summarise can cope with a
     short raw_text.
  4. Feed health (last_success/consecutive_failures/active) is updated for
     every feed on every run, and every article insert commits immediately, so
     a crash or Ctrl-C mid-run loses at most the one in-flight item -- nothing
     already written needs to be redone.
  5. Auto-recovery (brief feature #5): a feed that's been auto-deactivated
     after MAX_CONSECUTIVE_FAILURES isn't fetched forever after -- it's
     deliberately NOT excluded from every future run the way v1's
     mark_dead_feeds.py excluded feeds permanently. Instead, up to
     `run.recovery_probes_per_run` of the longest-untried inactive feeds are
     retried each run, but no more often than once every
     `run.recovery_check_interval_hours` per feed (feeds.last_attempt tracks
     this). A successful probe reactivates the feed and resets its streak;
     a failed probe just updates last_attempt so it isn't retried again
     until the next cooldown window.

Usage: python -m pipeline.fetch  (or `python run.py fetch`)
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import mktime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import httpx
import trafilatura

from pipeline.config import get_config
from pipeline.db import get_connection, init_db

USER_AGENT = "econ-news-aggregator/0.1 (+https://github.com/; RSS fetcher)"

# Consecutive fetch failures after which a feed is auto-deactivated (kept in
# the db, flagged, never deleted -- a human can reactivate it later).
MAX_CONSECUTIVE_FAILURES = 14

# If a feed entry's own summary/content is shorter than this, we don't trust
# it as a usable article body and fall back to fetching+extracting the page.
MIN_USABLE_BODY_CHARS = 300

# Raw extracted article text is capped at this many characters -- plenty for
# the downstream summariser, and keeps db rows/LLM context bounded.
MAX_RAW_TEXT_CHARS = 8000

# Tracking-parameter prefixes/names stripped from article URLs before hashing,
# so the same article reached via different campaign links hashes identically.
TRACKING_PARAM_PREFIXES = ("utm_", "ito=", "ns_", "icid", "mc_", "cmp")
TRACKING_PARAM_NAMES = {
    "fbclid", "gclid", "msclkid", "mkt_tok", "ref", "refid", "cmpid",
    "spref", "share", "source", "smid", "ocid", "wt_mc", "cmp", "utm",
}


@dataclass
class RunStats:
    feeds_tried: int = 0
    feeds_ok: int = 0
    feeds_failed: int = 0
    feeds_recovered: int = 0
    new_articles: int = 0


def normalise_url(url: str) -> str:
    """Strips tracking query params and the fragment so equivalent article
    links (same article, different campaign tag) normalise to the same URL.
    """
    parts = urlsplit(url)
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAM_NAMES
        and not any(k.lower().startswith(p) for p in TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", query, ""))


def hash_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def parse_published(entry: dict) -> str | None:
    """Robustly pulls a published/updated timestamp off a feedparser entry.

    Returns an ISO8601 string, or None if the entry has nothing usable (the
    caller falls back to fetch time).
    """
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return datetime.fromtimestamp(mktime(struct), tz=timezone.utc).isoformat()
            except (OverflowError, ValueError):
                continue
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                continue
    return None


def entry_body_text(entry: dict) -> str:
    """Best-effort plain-ish text from whatever body the feed entry supplies."""
    if entry.get("content"):
        value = entry["content"][0].get("value", "")
        if value:
            return value
    return entry.get("summary", "") or ""


async def fetch_feed(client: httpx.AsyncClient, feed: dict, timeout: float) -> tuple[bool, feedparser.FeedParserDict | None, dict]:
    """Conditionally GETs a feed. Returns (changed, parsed_or_None, new_cache_headers).

    changed=False means a 304 Not Modified (or an unparsable-but-non-error
    response) -- caller should still count it as a success for feed health.
    """
    headers = {}
    if feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]

    resp = await client.get(feed["url"], headers=headers, timeout=timeout, follow_redirects=True)

    if resp.status_code == 304:
        return False, None, {}

    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    new_headers = {
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
    }
    return True, parsed, new_headers


async def extract_article_text(client: httpx.AsyncClient, url: str, timeout: float) -> str | None:
    """Fetches `url` and pulls out article body text with trafilatura.

    Returns None (never raises) if the fetch or extraction fails -- callers
    keep the article title-only rather than dropping it.
    """
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    try:
        return trafilatura.extract(resp.text)
    except Exception:
        return None


def article_exists(conn, url_hash: str) -> bool:
    row = conn.execute("SELECT 1 FROM articles WHERE url_hash = ?", (url_hash,)).fetchone()
    return row is not None


def classify_fetch_error(exc: Exception) -> str:
    """Buckets a feed-fetch failure into the same taxonomy v1 used
    (http_<code>/timeout/dns/ssl/connection), so feed-health tracking (Phase 6)
    can tell "this feed moved" apart from "this feed had one bad night."
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc)
        if "NameResolutionError" in msg or "getaddrinfo failed" in msg or "Name or service not known" in msg:
            return "dns"
        if "SSL" in msg or isinstance(exc.__cause__, ssl.SSLError):
            return "ssl"
        return "connection"
    return "other"


async def process_feed(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    feed: dict,
    fetch_timeout: float,
    stats: RunStats,
    stats_lock: asyncio.Lock,
    articles_remaining: list[int],
    articles_remaining_lock: asyncio.Lock,
) -> None:
    async with semaphore:
        now = datetime.now(timezone.utc).isoformat()
        try:
            changed, parsed, new_headers = await fetch_feed(client, feed, fetch_timeout)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            error_type = classify_fetch_error(exc)
            with get_connection() as conn:
                conn.execute(
                    """
                    UPDATE feeds
                    SET consecutive_failures = consecutive_failures + 1,
                        active = CASE WHEN consecutive_failures + 1 >= ? THEN 0 ELSE active END,
                        last_error_type = ?,
                        last_attempt = ?
                    WHERE id = ?
                    """,
                    (MAX_CONSECUTIVE_FAILURES, error_type, now, feed["id"]),
                )
            async with stats_lock:
                stats.feeds_failed += 1
            return

        # Success (including 304 Not Modified) -- reset failure streak and
        # reactivate if this was a recovery probe on a previously-inactive feed.
        with get_connection() as conn:
            update_fields = {
                "last_success": now,
                "last_attempt": now,
                "consecutive_failures": 0,
                "last_error_type": None,
                "active": 1,
            }
            if changed:
                update_fields["etag"] = new_headers.get("etag")
                update_fields["last_modified"] = new_headers.get("last_modified")
            set_clause = ", ".join(f"{k} = ?" for k in update_fields)
            conn.execute(
                f"UPDATE feeds SET {set_clause} WHERE id = ?",
                (*update_fields.values(), feed["id"]),
            )
        async with stats_lock:
            stats.feeds_ok += 1
            if not feed["active"]:
                stats.feeds_recovered += 1

        if not changed or parsed is None:
            return

        for entry in parsed.entries:
            link = entry.get("link")
            if not link:
                continue
            url = normalise_url(link)
            url_hash = hash_url(url)

            with get_connection() as conn:
                if article_exists(conn, url_hash):
                    continue

            async with articles_remaining_lock:
                if articles_remaining[0] <= 0:
                    return
                articles_remaining[0] -= 1

            title = entry.get("title", "") or ""
            published = parse_published(entry)
            body = entry_body_text(entry)
            raw_text = body

            if len(body) < MIN_USABLE_BODY_CHARS:
                extracted = await extract_article_text(client, url, fetch_timeout)
                if extracted:
                    raw_text = extracted

            raw_text = (raw_text or "")[:MAX_RAW_TEXT_CHARS] or None
            fetched_at = datetime.now(timezone.utc).isoformat()

            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO articles
                        (url_hash, feed_id, title, url, published, fetched, raw_text, processed_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'fetched')
                    ON CONFLICT(url_hash) DO NOTHING
                    """,
                    (url_hash, feed["id"], title, url, published, fetched_at, raw_text),
                )

            async with stats_lock:
                stats.new_articles += 1


async def fetch_all(
    concurrency: int, timeout: float, max_articles: int,
    recovery_interval_hours: float, recovery_probes_per_run: int,
) -> RunStats:
    cooldown_cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=recovery_interval_hours)
    ).isoformat()
    with get_connection() as conn:
        feeds = [dict(row) for row in conn.execute("SELECT * FROM feeds WHERE active = 1")]
        recovering = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM feeds
                WHERE active = 0 AND (last_attempt IS NULL OR last_attempt <= ?)
                ORDER BY last_attempt IS NOT NULL, last_attempt ASC
                LIMIT ?
                """,
                (cooldown_cutoff, recovery_probes_per_run),
            )
        ]
    feeds += recovering

    stats = RunStats(feeds_tried=len(feeds))
    if not feeds:
        return stats

    semaphore = asyncio.Semaphore(concurrency)
    stats_lock = asyncio.Lock()
    articles_remaining = [max_articles]
    articles_remaining_lock = asyncio.Lock()

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        tasks = [
            process_feed(
                client, semaphore, feed, timeout, stats, stats_lock,
                articles_remaining, articles_remaining_lock,
            )
            for feed in feeds
        ]
        await asyncio.gather(*tasks)

    return stats


def main() -> RunStats:
    init_db()
    cfg = get_config()
    concurrency = cfg["run"].get("fetch_concurrency", 30)
    timeout = cfg["run"].get("fetch_timeout_seconds", 15)
    max_articles = cfg["run"].get("max_articles_per_run", 300)
    recovery_interval_hours = cfg["run"].get("recovery_check_interval_hours", 24)
    recovery_probes_per_run = cfg["run"].get("recovery_probes_per_run", 15)

    print(
        f"Fetching active feeds ({concurrency} at a time, {timeout}s timeout, max {max_articles} new articles), "
        f"probing up to {recovery_probes_per_run} inactive feed(s) for recovery..."
    )
    started = datetime.now(timezone.utc)
    stats = asyncio.run(
        fetch_all(concurrency, timeout, max_articles, recovery_interval_hours, recovery_probes_per_run)
    )
    duration = (datetime.now(timezone.utc) - started).total_seconds()

    print(
        f"Done in {duration:.1f}s: "
        f"{stats.feeds_tried} feeds tried, {stats.feeds_ok} ok ({stats.feeds_recovered} recovered), "
        f"{stats.feeds_failed} failed, {stats.new_articles} new articles."
    )
    sys.stdout.flush()
    return stats


if __name__ == "__main__":
    main()
