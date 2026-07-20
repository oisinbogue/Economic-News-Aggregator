"""One-off backfill for articles.image on rows fetched before that column
existed (or where feed-time extraction found nothing). Safe to re-run --
only touches rows where image IS NULL, and a failed lookup just leaves the
row NULL for the next run rather than erroring out.

Usage: python -m pipeline.backfill_images
"""

from __future__ import annotations

import asyncio
import sys

import httpx
from trafilatura.metadata import extract_metadata

from pipeline.db import get_connection, init_db
from pipeline.fetch import USER_AGENT

FETCH_TIMEOUT = 15.0
CONCURRENCY = 10


async def _fetch_image(client: httpx.AsyncClient, semaphore: asyncio.Semaphore, url_hash: str, url: str) -> tuple[str, str | None]:
    async with semaphore:
        try:
            resp = await client.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException):
            return url_hash, None
        try:
            meta = extract_metadata(resp.text, default_url=url)
            return url_hash, (meta.image if meta else None)
        except Exception:
            return url_hash, None


async def backfill() -> int:
    with get_connection() as conn:
        rows = [
            (row["url_hash"], row["url"])
            for row in conn.execute("SELECT url_hash, url FROM articles WHERE image IS NULL")
        ]
    if not rows:
        return 0

    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(*(_fetch_image(client, semaphore, h, u) for h, u in rows))

    updated = 0
    with get_connection() as conn:
        for url_hash, image in results:
            if image:
                conn.execute("UPDATE articles SET image = ? WHERE url_hash = ?", (image, url_hash))
                updated += 1
    return updated


def main() -> int:
    init_db()
    print("Backfilling thumbnails for articles without one...")
    updated = asyncio.run(backfill())
    print(f"Done: {updated} article(s) got a thumbnail.")
    sys.stdout.flush()
    return updated


if __name__ == "__main__":
    main()
