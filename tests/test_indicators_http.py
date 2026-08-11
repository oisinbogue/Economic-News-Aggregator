"""Tests indicators/http.py's retry/backoff behaviour against a mocked
transport -- never live network (INDICATORS_BRIEF.md §11: "against a mocked
429 response"). Uses asyncio.run()-in-a-sync-test, matching
tests/test_indicators_providers.py's convention (pytest-asyncio isn't a
project dependency).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from indicators import http as http_mod


async def _no_sleep(*_args, **_kwargs) -> None:
    return None


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(http_mod.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await http_mod.get(client, "https://example.com/x")

    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert calls["n"] == 3


def test_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(http_mod.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await http_mod.get(client, "https://example.com/x")

    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(http_mod.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429)

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await http_mod.get(client, "https://example.com/x", max_retries=2, base_delay=0.01)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert calls["n"] == 3  # initial attempt + 2 retries


def test_does_not_retry_non_retryable_4xx(monkeypatch):
    monkeypatch.setattr(http_mod.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await http_mod.get(client, "https://example.com/x")

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert calls["n"] == 1  # no retry at all for a non-retryable status
