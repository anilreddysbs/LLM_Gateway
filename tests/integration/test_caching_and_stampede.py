"""Integration tests for response caching, Cache-Control headers, and stampede protection."""

import asyncio

import pytest
from httpx import AsyncClient

from llm_gateway.db.models import ApiKey
from llm_gateway.providers.factory import get_provider_factory
from llm_gateway.providers.mock import MockProvider


@pytest.fixture(autouse=True)
def setup_mock_provider():
    factory = get_provider_factory()
    mock_prov = MockProvider(fixed_text="Deterministic mock completion")
    factory.register_provider("mock", mock_prov)
    yield mock_prov
    mock_prov.reset()


@pytest.mark.asyncio
async def test_cache_miss_then_hit(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
    setup_mock_provider: MockProvider,
):
    """First low-temperature request results in cache MISS; second results in cache HIT."""
    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "Cache test prompt"}],
        "temperature": 0.1,  # <= 0.3 threshold
    }

    # First request: Cache MISS
    res1 = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert res1.status_code == 200
    assert res1.headers["X-Gateway-Cache"] == "MISS"
    assert setup_mock_provider.call_count == 1

    # Second request: Cache HIT (provider call count remains 1)
    res2 = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.headers["X-Gateway-Cache"] == "HIT"
    assert (
        res2.json()["choices"][0]["message"]["content"]
        == res1.json()["choices"][0]["message"]["content"]
    )
    assert setup_mock_provider.call_count == 1


@pytest.mark.asyncio
async def test_cache_control_no_cache_and_no_store(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
    setup_mock_provider: MockProvider,
):
    """'Cache-Control: no-cache' bypasses read; 'no-store' skips writing to cache."""
    _, raw_key = valid_api_key
    base_headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "Header test prompt"}],
        "temperature": 0.0,
    }

    # 1. Warm cache
    r1 = await client.post("/v1/chat/completions", json=payload, headers=base_headers)
    assert r1.status_code == 200
    assert r1.headers["X-Gateway-Cache"] == "MISS"
    assert setup_mock_provider.call_count == 1

    # 2. no-cache forces fresh provider call despite entry being in cache
    no_cache_headers = {**base_headers, "Cache-Control": "no-cache"}
    r2 = await client.post("/v1/chat/completions", json=payload, headers=no_cache_headers)
    assert r2.status_code == 200
    assert r2.headers["X-Gateway-Cache"] == "MISS"
    assert setup_mock_provider.call_count == 2

    # 3. no-store test: request on fresh prompt with no-store
    fresh_payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "Never store this"}],
        "temperature": 0.0,
    }
    no_store_headers = {**base_headers, "Cache-Control": "no-store"}
    r3 = await client.post("/v1/chat/completions", json=fresh_payload, headers=no_store_headers)
    assert r3.status_code == 200
    assert r3.headers["X-Gateway-Cache"] == "MISS"

    # Subsequent request on same prompt must still MISS because it was not stored
    r4 = await client.post("/v1/chat/completions", json=fresh_payload, headers=base_headers)
    assert r4.status_code == 200
    assert r4.headers["X-Gateway-Cache"] == "MISS"


@pytest.mark.asyncio
async def test_cache_stampede_protection(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
    setup_mock_provider: MockProvider,
):
    """Concurrent identical requests result in only ONE provider call; others wait and receive HIT."""
    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "Stampede concurrency test prompt"}],
        "temperature": 0.0,
    }

    # Add 100ms artificial latency to ensure lock contention
    setup_mock_provider.latency_seconds = 0.1

    # Fire 5 concurrent identical requests simultaneously
    tasks = [client.post("/v1/chat/completions", json=payload, headers=headers) for _ in range(5)]
    responses = await asyncio.gather(*tasks)

    # All 5 requests must succeed
    assert all(r.status_code == 200 for r in responses)

    # Provider should only have been called ONCE due to distributed lock
    assert setup_mock_provider.call_count == 1

    cache_headers = [r.headers["X-Gateway-Cache"] for r in responses]
    # Exactly one MISS (lock owner) and four HITs (waiters)
    assert cache_headers.count("MISS") == 1
    assert cache_headers.count("HIT") == 4
