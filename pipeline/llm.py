"""Shared LLM API plumbing: auth, throttling, and the raw call.

Split out of pipeline/summarize.py so pipeline/curate.py (Phase 3, LLM top-10
curation) shares the same rate limiter instead of each module independently
guessing at a safe pace.

Provider-agnostic by design (formerly pipeline/cerebras.py, hardcoded to
Cerebras): base_url, model, and the API key's env var name all come from
config.yaml's llm: block, so swapping providers -- which happened once
already, 2026-08-26, when Cerebras force-migrated free accounts to a $0
pay-as-you-go balance -- is a config change, not a code change, as long as
the new provider speaks the OpenAI-compatible chat/completions shape (Gemini,
Groq, and most others do).

Usage: from pipeline.llm import call, get_api_key, load_dotenv
"""

from __future__ import annotations

import os
import time

import httpx

from pipeline.config import get_config, resolve_path


def load_dotenv() -> None:
    """Minimal .env loader so the configured API key env var works locally
    without adding a dependency. GitHub Actions supplies the same var via a
    repo secret -- no .env file involved there, so this is a no-op in CI."""
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
    env_var = get_config()["llm"]["api_key_env"]
    api_key = os.environ.get(env_var)
    if not api_key:
        raise RuntimeError(
            f"{env_var} is not set. Add it to .env locally, or as the "
            f"{env_var} GitHub Actions secret in CI."
        )
    return api_key


# Stay under the configured requests-per-minute across the whole process
# rather than tracking a rolling window: 60/rpm, plus a margin for
# clock/latency slack.
_last_call_at = 0.0


def _min_seconds_between_calls() -> float:
    rpm = get_config()["llm"]["rpm"]
    return 60.0 / rpm + 1.0


def _throttle() -> None:
    global _last_call_at
    min_seconds = _min_seconds_between_calls()
    elapsed = time.monotonic() - _last_call_at
    if elapsed < min_seconds:
        time.sleep(min_seconds - elapsed)
    _last_call_at = time.monotonic()


# 429s happen even with in-process throttling -- e.g. a prior stage in the
# same workflow run (or a separate manual run earlier that day) can exhaust
# the per-minute or daily budget right before this call. Retry a few times
# with backoff instead of failing the whole pipeline step outright.
MAX_429_RETRIES = 3
RETRY_BACKOFF_SECONDS = 20.0


def call(client: httpx.Client, api_key: str, model: str, prompt: str, max_tokens: int, temperature: float = 0.3) -> str:
    """Raises RuntimeError (not KeyError) if the model produced no content.

    Some models (reasoning models -- Cerebras's old gpt-oss-120b was one) draw
    hidden "thinking" tokens from the same max_tokens budget as the visible
    answer, and a prompt that makes the model "think" a lot can burn the
    whole budget before it writes any content -- finish_reason='length' with
    message.content missing entirely rather than merely short. Callers need
    generous max_tokens headroom for models like this, not just enough for
    the expected answer length.

    A 402 here means the provider account itself needs attention (expired
    free tier, unfunded pay-as-you-go balance, etc.) -- not something a
    retry fixes. It surfaces via resp.raise_for_status() same as any other
    4xx; check the configured provider's billing/account dashboard.
    """
    base_url = get_config()["llm"]["base_url"]
    resp = None
    for attempt in range(MAX_429_RETRIES + 1):
        _throttle()
        resp = client.post(
            base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=30,
        )
        if resp.status_code != 429 or attempt == MAX_429_RETRIES:
            break
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else RETRY_BACKOFF_SECONDS * (attempt + 1)
        time.sleep(delay)
    resp.raise_for_status()
    choice = resp.json()["choices"][0]
    content = choice.get("message", {}).get("content")
    if not content:
        raise RuntimeError(
            f"LLM returned no content (finish_reason={choice.get('finish_reason')!r}); "
            "likely ran out of max_tokens on hidden reasoning before writing an answer -- "
            "raise max_tokens at the call site."
        )
    return content.strip()
