"""Shared HTTP retry/backoff helper for indicators provider modules.

INDICATORS_BRIEF.md §8's "rate-limit safe" requirement (exponential backoff
on 429/5xx, never let a single provider abort the run) belongs in one place
rather than being re-implemented -- or forgotten -- per provider module.
Every provider (dbnomics/ecb/eurostat/ons/boe/stooq) calls `get()` here
instead of `client.get(...); resp.raise_for_status()` directly.

Concurrency caps and inter-request delays (the other half of "rate-limit
safe") are a per-run, cross-series concern -- indicators/fetch.py owns
those (one semaphore + one minimum delay per provider name, from
config.yaml's `indicators.provider_concurrency` / `provider_delay_seconds`),
not this module, which only knows about a single request at a time.
"""

from __future__ import annotations

import asyncio
import random

import httpx

# 429 (rate limited) and the 5xx family (server-side trouble, often
# transient on a free public API). Any other status -- including other
# 4xx, which mean "this request is wrong" and won't succeed on retry --
# raises immediately via resp.raise_for_status().
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

DEFAULT_MAX_RETRIES = 4
DEFAULT_BASE_DELAY = 1.0


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    **kwargs,
) -> httpx.Response:
    """Issues one HTTP request with exponential backoff + jitter on 429/5xx.

    Delay before retry attempt N (0-indexed) is `base_delay * 2**N`,
    jittered by +/-25% so many series backing off from the same provider at
    once don't retry in lockstep -- honours a numeric `Retry-After` header
    when the response supplies one, since that's the server's own request
    rather than a guess. Up to `max_retries` retries (so `max_retries + 1`
    attempts total); the final attempt's response -- success or failure --
    is returned/raised via `raise_for_status()` same as any other, so a
    provider that exhausts retries still surfaces as one ordinary exception
    to indicators/fetch.py's per-series try/except, not a special case.
    """
    attempt = 0
    while True:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code not in RETRYABLE_STATUS or attempt >= max_retries:
            resp.raise_for_status()
            return resp

        retry_after = resp.headers.get("Retry-After")
        delay = base_delay * (2**attempt)
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                pass
        delay *= 1 + random.uniform(-0.25, 0.25)

        await asyncio.sleep(max(delay, 0.0))
        attempt += 1


async def get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    return await request(client, "GET", url, **kwargs)
