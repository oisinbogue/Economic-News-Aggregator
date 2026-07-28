"""Re-validates every feed in config/feeds.yaml and writes the results back
into that file -- config/feeds.yaml is the pipeline's single source of truth
for which feeds exist and whether they're active (see pipeline.reconcile_feeds,
which syncs the `feeds` db table from it on every run). This script doesn't
touch the db at all; run pipeline.reconcile_feeds afterwards to pick up any
status changes.

For each row (tested at its `original_url` if one is recorded -- i.e. the
earliest known URL for that source -- else its current `url`):
  1. GET the URL (following redirects) with a short timeout.
  2. Try to parse the response body as RSS/Atom with feedparser.
  3. Classify as:
       ok         - parsed cleanly at the URL as given
       redirected - parsed cleanly, but the final URL differs from the one
                    tested (recorded as `original_url`, so future runs keep
                    retesting from the true original rather than drifting)
       dead       - request failed, or the body isn't a usable feed. For
                    these we make one extra attempt: fetch the site's
                    homepage and look for a <link rel="alternate" ...>
                    tag advertising a feed (RSS auto-discovery), and test
                    that URL too.

Each row's `url`, `original_url`, `validation_status`, `active`, and
`validation_notes` are updated in place; `name`/`country`/`language`/
`topic_hint`/`previously_disabled` are left untouched (human-curated).

Usage: python -m pipeline.validate_feeds
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
import yaml

from pipeline.config import get_config, resolve_path
from pipeline.fetch import USER_AGENT

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
    status: str          # ok / redirected / discovered / dead
    resolved_url: str    # URL actually used going forward (may equal the tested URL)
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
    url: str,
    timeout: float,
) -> ValidationResult:
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
            return ValidationResult(status=status, resolved_url=final_url)

        # Not a feed (or request failed outright) -- try RSS autodiscovery.
        discovered = await _attempt_autodiscovery(client, url, timeout)
        if discovered:
            return ValidationResult(
                status="discovered", resolved_url=discovered,
                notes="found via homepage <link rel=alternate>",
            )

        return ValidationResult(
            status="dead", resolved_url=url,
            notes=fetch_error or "no entries found and no feed link discovered",
        )


async def validate_all(urls: list[str], concurrency: int, timeout: float) -> list[ValidationResult]:
    semaphore = asyncio.Semaphore(concurrency)
    # Same UA the real fetcher sends (pipeline.fetch.USER_AGENT), so a feed
    # that validates here will also fetch there. Validating under a different
    # agent than we fetch with is how ~100 perfectly good feeds ended up
    # marked dead: the WAF rejected the validator, not the feed.
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        tasks = [validate_one(client, semaphore, url, timeout) for url in urls]
        return await asyncio.gather(*tasks)


def apply_result(row: dict, tested_url: str, result: ValidationResult) -> None:
    row["validation_status"] = result.status
    row["active"] = result.status in ("ok", "redirected", "discovered")
    if result.notes:
        row["validation_notes"] = result.notes
    else:
        row.pop("validation_notes", None)

    row["url"] = result.resolved_url
    if result.resolved_url != tested_url:
        row["original_url"] = tested_url
    else:
        row.pop("original_url", None)


def main() -> None:
    cfg = get_config()
    feeds_yaml = resolve_path(cfg["paths"]["feeds_yaml"])
    concurrency = cfg["validate_feeds"]["concurrency"]
    timeout = cfg["validate_feeds"]["timeout_seconds"]

    if not feeds_yaml.exists():
        print(f"feeds.yaml not found at {feeds_yaml}", file=sys.stderr)
        sys.exit(1)

    with open(feeds_yaml, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    regions = doc.get("regions") or {}

    rows = [row for region_rows in regions.values() for row in (region_rows or [])]
    if not rows:
        print("feeds.yaml has no rows to validate.")
        return

    tested_urls = [(row.get("original_url") or row["url"]).strip() for row in rows]

    print(f"Validating {len(rows)} feeds ({concurrency} at a time, {timeout}s timeout)...")
    started = datetime.now(timezone.utc)
    results = asyncio.run(validate_all(tested_urls, concurrency, timeout))
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    counts: dict[str, int] = {}
    for row, tested_url, result in zip(rows, tested_urls, results):
        apply_result(row, tested_url, result)
        counts[result.status] = counts.get(result.status, 0) + 1
    print(f"Done in {elapsed:.1f}s: {counts}")

    with open(feeds_yaml, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    print(f"Wrote {feeds_yaml}")
    print("Run `python -m pipeline.reconcile_feeds` to sync these changes into the db.")


if __name__ == "__main__":
    main()
