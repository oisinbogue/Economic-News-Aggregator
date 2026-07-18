"""Shared Cerebras API plumbing: auth, throttling, and the raw call.

Split out of pipeline/summarize.py so pipeline/curate.py (Phase 3, LLM top-10
curation) shares the same rate limiter instead of each module independently
guessing at a safe pace.

Usage: from pipeline.cerebras import call, get_api_key, load_dotenv
"""

from __future__ import annotations

import os
import time

import httpx

from pipeline.config import resolve_path

CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"

# Free tier is 5 req/min (also 30K tokens/min, 1M tokens/day, 8,192-token
# context -- all satisfied by capping prompt size at each call site). Stay
# under 5/min across the whole process rather than tracking a rolling
# window: 60/5 = 12s, plus a margin for clock/latency slack.
MIN_SECONDS_BETWEEN_CALLS = 13.0

_last_call_at = 0.0


def load_dotenv() -> None:
    """Minimal .env loader so CEREBRAS_API_KEY works locally without adding
    a dependency. GitHub Actions supplies the same var via a repo secret --
    no .env file involved there, so this is a no-op in CI."""
    env_path = resolve_path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def get_api_key() -> str:
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "CEREBRAS_API_KEY is not set. Add it to .env locally, or as the "
            "CEREBRAS_API_KEY GitHub Actions secret in CI."
        )
    return api_key


def _throttle() -> None:
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
    _last_call_at = time.monotonic()


def call(client: httpx.Client, api_key: str, model: str, prompt: str, max_tokens: int, temperature: float = 0.3) -> str:
    """Raises RuntimeError (not KeyError) if the model produced no content.

    gpt-oss-120b (see config.yaml's llm.model comment) is a reasoning model:
    its hidden "reasoning" tokens are drawn from the same max_tokens budget
    as the visible answer, and a prompt that makes it "think" a lot can burn
    the whole budget before it writes any content -- finish_reason='length'
    with message.content missing entirely rather than merely short. Callers
    need generous max_tokens headroom for this model, not just enough for
    the expected answer length.
    """
    _throttle()
    resp = client.post(
        CEREBRAS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=30,
    )
    resp.raise_for_status()
    choice = resp.json()["choices"][0]
    content = choice.get("message", {}).get("content")
    if not content:
        raise RuntimeError(
            f"Cerebras returned no content (finish_reason={choice.get('finish_reason')!r}); "
            "likely ran out of max_tokens on hidden reasoning before writing an answer -- "
            "raise max_tokens at the call site."
        )
    return content.strip()
