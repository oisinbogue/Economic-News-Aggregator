"""Validates every feed in feeds.csv before the pipeline relies on it.

For each row:
  1. GET the URL (following redirects) with a short timeout.
  2. Try to parse the response body as RSS/Atom with feedparser.
  3. Classify as:
       ok         - parsed cleanly at the URL as given
       redirected - parsed cleanly, but the final URL differs from the one
                    in feeds.csv (we record the new URL and import that
                    instead, so future runs hit the live location directly)
       dead       - request failed, or the body isn't a usable feed. For
                    these we make one extra attempt: fetch the site's
                    homepage and look for a <link rel="alternate" ...>
                    tag advertising a feed (RSS auto-discovery), and test
                    that URL too.

Output: data/feeds_validated.csv (feeds.csv columns + status/resolved_url/
notes), and every row classified ok/redirected/discovered is upserted into
the feeds table as active.

Usage: python -m pipeline.validate_feeds
"""

from __future__ import annotations

import asyncio
import csv
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from pipeline.config import get_config, resolve_path
from pipeline.db import get_connection, init_db

# Feed mime-types worth treating as "this link is a feed" during autodiscovery.
FEED_LINK_TYPES = {
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
}


class _FeedLinkFinder(HTMLParser):
    """Minimal HTML parser that collects <link rel="alternate" href=...> feed URLs.

    Using stdlib html.parser instead of a third-party HTML library keeps us
    within the fixed dependency list.
    """

    def __init__(self) -> None:
        super().__init__()
        self.feed_hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        rel = attr_dict.get("rel", "").lower()
        type_ = attr_dict.get("type", "").lower()
        href = attr_dict.get("href", "")
        if "alternate" in rel and type_ in FEED_LINK_TYPES and href:
            self.feed_hrefs.append(href)


@dataclass
class ValidationResult:
    url: str
    name: str
    country: str
    language: str
    topic_hint: str
    status: str          # ok / redirected / discovered / dead
    resolved_url: str    # URL actually used going forward (may equal url)
    notes: str = ""


async def _try_parse_as_feed(client: httpx.AsyncClient, url: str, timeout: float) -> tuple[bool, str]:
    """Fetches `url` and checks whether feedparser can find entries in it.

    Returns (looks_like_feed, final_url_after_redirects).
    """
    resp = await client.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.text)
    # bozo=1 just means "not strictly well-formed"; many real-world feeds
    # trip it while still having perfectly usable entries, so we only treat
    # "no entries at all" as a failure.
    looks_like_feed = len(parsed.entries) > 0
    return looks_like_feed, str(resp.url)


async def _attempt_autodiscovery(client: httpx.AsyncClient, original_url: str, timeout: float) -> str | None:
    """For a dead feed, fetches the site homepage and looks for a <link rel="alternate"> feed.

    Returns a candidate feed URL if one was found AND validated, else None.
    """
    parsed_original = urlparse(original_url)
    homepage = f"{parsed_original.scheme}://{parsed_original.netloc}/"
    try:
        resp = await client.get(homepage, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    finder = _FeedLinkFinder()
    finder.feed(resp.text)
    for href in finder.feed_hrefs:
        candidate = urljoin(str(resp.url), href)
        try:
            looks_like_feed, final_url = await _try_parse_as_feed(client, candidate, timeout)
        except (httpx.HTTPError, httpx.TimeoutException):
            continue
        if looks_like_feed:
            return final_url
    return None


async def validate_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    row: dict,
    timeout: float,
) -> ValidationResult:
    url = row["url"].strip()
    async with semaphore:
        try:
            looks_like_feed, final_url = await _try_parse_as_feed(client, url, timeout)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            looks_like_feed, final_url = False, url
            fetch_error = str(exc)
        else:
            fetch_error = ""

        if looks_like_feed:
            status = "ok" if final_url == url else "redirected"
            return ValidationResult(
                url=url, name=row["name"], country=row["country"],
                language=row["language"], topic_hint=row["topic_hint"],
                status=status, resolved_url=final_url,
            )

        # Not a feed (or request failed outright) -- try RSS autodiscovery.
        discovered = await _attempt_autodiscovery(client, url, timeout)
        if discovered:
            return ValidationResult(
                url=url, name=row["name"], country=row["country"],
                language=row["language"], topic_hint=row["topic_hint"],
                status="discovered", resolved_url=discovered,
                notes="found via homepage <link rel=alternate>",
            )

        return ValidationResult(
            url=url, name=row["name"], country=row["country"],
            language=row["language"], topic_hint=row["topic_hint"],
            status="dead", resolved_url=url,
            notes=fetch_error or "no entries found and no feed link discovered",
        )


async def validate_all(rows: list[dict], concurrency: int, timeout: float) -> list[ValidationResult]:
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers={"User-Agent": "econ-news-aggregator/0.1 (+feed-validator)"}) as client:
        tasks = [validate_one(client, semaphore, row, timeout) for row in rows]
        return await asyncio.gather(*tasks)


def import_valid_feeds(results: list[ValidationResult]) -> int:
    """Upserts every ok/redirected/discovered result into the feeds table. Returns count imported."""
    importable = [r for r in results if r.status in ("ok", "redirected", "discovered")]
    with get_connection() as conn:
        for r in importable:
            conn.execute(
                """
                INSERT INTO feeds (url, name, country, language, topic_hint, active)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(url) DO UPDATE SET
                    name=excluded.name,
                    country=excluded.country,
                    language=excluded.language,
                    topic_hint=excluded.topic_hint,
                    active=1
                """,
                (r.resolved_url, r.name, r.country, r.language, r.topic_hint),
            )
    return len(importable)


def write_validated_csv(results: list[ValidationResult], out_path) -> None:
    fieldnames = list(asdict(results[0]).keys()) if results else [
        "url", "name", "country", "language", "topic_hint", "status", "resolved_url", "notes",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def main() -> None:
    cfg = get_config()
    feeds_csv = resolve_path(cfg["paths"]["feeds_csv"])
    out_csv = resolve_path(cfg["paths"]["feeds_validated_csv"])
    concurrency = cfg["validate_feeds"]["concurrency"]
    timeout = cfg["validate_feeds"]["timeout_seconds"]

    if not feeds_csv.exists():
        print(f"feeds.csv not found at {feeds_csv}", file=sys.stderr)
        sys.exit(1)

    with open(feeds_csv, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("url", "").strip()]

    if not rows:
        print("feeds.csv has no rows to validate.")
        return

    print(f"Validating {len(rows)} feeds ({concurrency} at a time, {timeout}s timeout)...")
    started = datetime.now(timezone.utc)
    results = asyncio.run(validate_all(rows, concurrency, timeout))
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"Done in {elapsed:.1f}s: {counts}")

    write_validated_csv(results, out_csv)
    print(f"Wrote {out_csv}")

    init_db()
    imported = import_valid_feeds(results)
    print(f"Imported/updated {imported} active feeds in the database.")


if __name__ == "__main__":
    main()
