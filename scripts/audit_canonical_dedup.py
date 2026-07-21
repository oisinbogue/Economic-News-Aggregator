"""One-off audit: how many already-stored articles would collide under the
new canonical-URL dedup scheme (see pipeline.fetch)?

Re-fetches every stored article's URL, extracts its <link rel="canonical">
via trafilatura (same as pipeline.fetch does live), and checks whether two or
more *distinct* existing rows would now hash to the same canonical URL. This
does NOT modify the database or merge anything -- it only reports counts for
human review.

Usage: python -m scripts.audit_canonical_dedup [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

import httpx
from trafilatura.metadata import extract_metadata

from pipeline.db import get_connection
from pipeline.fetch import USER_AGENT, hash_url, normalise_url

CONCURRENCY = 20
TIMEOUT = 15.0


async def canonical_for(client: httpx.AsyncClient, url: str) -> tuple[bool, str | None]:
    """Returns (fetched_ok, canonical_url_or_None). canonical_url is None
    both when the fetch/extract failed AND when it succeeded but the page's
    canonical URL is the same as `url` -- fetched_ok distinguishes the two.
    """
    try:
        resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return False, None
    try:
        meta = extract_metadata(resp.text, default_url=url)
    except Exception:
        return False, None
    if meta and meta.url and meta.url != url:
        return True, normalise_url(meta.url)
    return True, None


async def audit(limit: int | None) -> None:
    with get_connection() as conn:
        query = "SELECT url_hash, url FROM articles"
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = [(row["url_hash"], row["url"]) for row in conn.execute(query)]

    print(f"Auditing {len(rows)} stored articles for canonical-URL collisions...")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    canonical_by_hash: dict[str, str | None] = {}
    fetch_failures = 0
    lock = asyncio.Lock()

    async def worker(client: httpx.AsyncClient, url_hash: str, url: str) -> None:
        nonlocal fetch_failures
        async with semaphore:
            fetched_ok, canonical = await canonical_for(client, url)
        async with lock:
            canonical_by_hash[url_hash] = canonical
            if not fetched_ok:
                fetch_failures += 1

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        await asyncio.gather(*(worker(client, h, u) for h, u in rows))

    # Group original hashes by (a) their own hash and (b) their canonical
    # hash, if a canonical URL was found and it differs. A collision is a
    # canonical hash shared by 2+ distinct original url_hashes.
    groups: dict[str, set[str]] = defaultdict(set)
    for url_hash, canonical_url in canonical_by_hash.items():
        key = hash_url(canonical_url) if canonical_url else url_hash
        groups[key].add(url_hash)

    collisions = {key: hashes for key, hashes in groups.items() if len(hashes) > 1}
    collided_articles = sum(len(hashes) for hashes in collisions.values())

    print(f"Fetched OK: {len(rows) - fetch_failures}/{len(rows)} ({fetch_failures} fetch/extract failures, treated as no-canonical)")
    print(f"Collision groups found: {len(collisions)}")
    print(f"Articles that would be affected (merged down to {len(collisions)} rows): {collided_articles}")

    if collisions:
        print("\nSample collision groups (url_hash sets sharing a canonical URL):")
        for key, hashes in list(collisions.items())[:20]:
            print(f"  canonical_hash={key[:12]}...  members={sorted(hashes)}")

    print("\nNo changes made -- this is a report only. Review before merging any rows.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Audit only the first N articles (for a quick check).")
    args = parser.parse_args()
    asyncio.run(audit(args.limit))


if __name__ == "__main__":
    sys.exit(main() or 0)
