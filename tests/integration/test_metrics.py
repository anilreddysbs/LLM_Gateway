"""Integration tests for Prometheus metrics scraping and telemetry counters."""

import pytest
from httpx import AsyncClient

from llm_gateway.db.models import ApiKey
from llm_gateway.observability.background import get_usage_queue


@pytest.mark.asyncio
async def test_metrics_endpoint_scrapes_prometheus_text(client: AsyncClient):
    """GET /metrics should return 200 with Prometheus text format."""
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    body = response.text
    # Core registered metrics must be present in scrape output
    assert "llm_gateway_requests_total" in body
    assert "llm_gateway_request_duration_seconds" in body
    assert "llm_gateway_tokens_total" in body
    assert "llm_gateway_cost_usd_total" in body
    assert "llm_gateway_cache_requests_total" in body
    assert "llm_gateway_circuit_breaker_state" in body


@pytest.mark.asyncio
async def test_metrics_increments_on_chat_completions(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Execution of chat completions should increment requests, tokens, cost, and cache metrics."""
    _, plaintext_key = valid_api_key
    queue = get_usage_queue()

    headers = {"Authorization": f"Bearer {plaintext_key}"}
    payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "Metric test prompt"}],
        "temperature": 0.1,
    }

    # 1. Non-streaming request (cache miss)
    resp1 = await client.post("/v1/chat/completions", headers=headers, json=payload)
    assert resp1.status_code == 200
    await queue.flush()

    # 2. Second request (cache hit)
    resp2 = await client.post("/v1/chat/completions", headers=headers, json=payload)
    assert resp2.status_code == 200
    assert resp2.headers.get("X-Gateway-Cache") == "HIT"
    await queue.flush()

    # 3. Scrape metrics
    metrics_res = await client.get("/metrics")
    assert metrics_res.status_code == 200
    text = metrics_res.text

    # Cache hit & miss metrics recorded
    assert 'llm_gateway_cache_requests_total{status="miss"}' in text
    assert 'llm_gateway_cache_requests_total{status="hit"}' in text

    # Requests total incremented
    assert 'llm_gateway_requests_total{model="mock-primary",provider="mock",status_code="200",streaming="false"}' in text
