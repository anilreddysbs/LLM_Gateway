"""Integration tests for Usage Logging, Background Queue, and Admin Usage/Stats Analytics."""

from decimal import Decimal

import pytest
from httpx import AsyncClient

from llm_gateway.db.models import ApiKey
from llm_gateway.db.repositories.usage_log_repo import UsageLogRepository
from llm_gateway.observability.background import UsageLogEntry, get_usage_queue


@pytest.mark.asyncio
async def test_usage_log_queue_direct_batch_flush(test_db_session):
    """Verify that entries enqueued into UsageLogQueue persist properly when flushed."""
    queue = get_usage_queue()

    entry = UsageLogEntry(
        request_id="req_test_batch_123",
        api_key_id=None,
        requested_model="fast",
        provider="mock",
        model="mock-primary",
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal("0.000025"),
        latency_ms=45.2,
        cache_hit=False,
        fallback_used=False,
        attempts=1,
        status_code=200,
        alias="fast",
        prompt_text="Hello world test",
        response_text="Echo response test",
    )

    success = queue.enqueue(entry)
    assert success is True

    # Flush directly to the database
    await queue.flush()

    # Query repository to verify persistence
    repo = UsageLogRepository(test_db_session)
    logs, total = await repo.query_logs(request_id="req_test_batch_123")

    assert total == 1
    log = logs[0]
    assert log.request_id == "req_test_batch_123"
    assert log.provider == "mock"
    assert log.model == "mock-primary"
    assert log.input_tokens == 100
    assert log.output_tokens == 50
    assert log.cost_usd == Decimal("0.000025")
    assert log.cache_hit is False
    assert log.fallback_used is False


@pytest.mark.asyncio
async def test_chat_completions_emits_usage_log(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Verify that a successful chat completion produces a usage log record."""
    _, plaintext_key = valid_api_key
    queue = get_usage_queue()

    headers = {"Authorization": f"Bearer {plaintext_key}"}
    payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "Tell me a joke."}],
        "temperature": 0.2,
    }

    # 1. Non-streaming call (cache miss)
    resp = await client.post("/v1/chat/completions", headers=headers, json=payload)
    assert resp.status_code == 200
    req_id_1 = resp.headers.get("X-Request-ID")
    assert req_id_1 is not None

    # Flush queue to write to DB
    await queue.flush()

    # 2. Query admin usage API to inspect the persisted log
    admin_headers = {"X-Admin-Key": "test-admin-key"}
    usage_res = await client.get(
        f"/admin/usage?request_id={req_id_1}",
        headers=admin_headers,
    )
    assert usage_res.status_code == 200
    data = usage_res.json()
    assert data["total"] == 1
    log_item = data["logs"][0]
    assert log_item["request_id"] == req_id_1
    assert log_item["model"] == "mock-primary"
    assert log_item["provider"] == "mock"
    assert log_item["cache_hit"] is False
    assert float(log_item["cost_usd"]) > 0
    assert log_item["streaming"] is False

    # 3. Second call with same prompt should be a Cache HIT
    resp_cached = await client.post("/v1/chat/completions", headers=headers, json=payload)
    assert resp_cached.status_code == 200
    assert resp_cached.headers.get("X-Gateway-Cache") == "HIT"
    req_id_2 = resp_cached.headers.get("X-Request-ID")

    await queue.flush()

    usage_res_2 = await client.get(
        f"/admin/usage?request_id={req_id_2}",
        headers=admin_headers,
    )
    assert usage_res_2.status_code == 200
    log_item_2 = usage_res_2.json()["logs"][0]
    assert log_item_2["cache_hit"] is True
    # Cache hit should have zero USD cost
    assert float(log_item_2["cost_usd"]) == 0.0


@pytest.mark.asyncio
async def test_streaming_chat_completions_emits_usage_log(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Verify that SSE streaming requests record proper usage telemetry."""
    _, plaintext_key = valid_api_key
    queue = get_usage_queue()

    headers = {"Authorization": f"Bearer {plaintext_key}"}
    payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "Stream me a response"}],
        "stream": True,
    }

    resp = await client.post("/v1/chat/completions", headers=headers, json=payload)
    assert resp.status_code == 200
    req_id = resp.headers.get("X-Request-ID")
    assert req_id is not None

    # Consume full stream body
    stream_content = resp.text
    assert "[DONE]" in stream_content

    # Flush background worker
    await queue.flush()

    admin_headers = {"X-Admin-Key": "test-admin-key"}
    usage_res = await client.get(
        f"/admin/usage?request_id={req_id}",
        headers=admin_headers,
    )
    assert usage_res.status_code == 200
    items = usage_res.json()["logs"]
    assert len(items) == 1
    stream_log = items[0]
    assert stream_log["streaming"] is True
    assert stream_log["status_code"] == 200
    assert stream_log["output_tokens"] > 0


@pytest.mark.asyncio
async def test_admin_stats_analytics_endpoint(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Verify that /admin/stats calculates aggregated totals, hit rates, latencies, and costs."""
    _, plaintext_key = valid_api_key
    queue = get_usage_queue()

    # Generate a few requests to populate stats
    headers = {"Authorization": f"Bearer {plaintext_key}"}
    for prompt in ["Question 1", "Question 2", "Question 3"]:
        res = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "mock-primary",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
        )
        assert res.status_code == 200

    await queue.flush()

    admin_headers = {"X-Admin-Key": "test-admin-key"}
    stats_res = await client.get("/admin/stats", headers=admin_headers)
    assert stats_res.status_code == 200
    stats = stats_res.json()

    assert stats["total_requests"] >= 3
    assert stats["error_rate"] == 0.0
    assert stats["p50_latency_ms"] >= 0.0
    assert stats["p95_latency_ms"] >= 0.0
    assert stats["p99_latency_ms"] >= 0.0
    assert stats["total_tokens"] > 0
    assert float(stats["total_cost_usd"]) > 0.0

    # Provider & model cost breakdowns
    assert "mock" in stats["cost_by_provider"]
    assert "mock-primary" in stats["cost_by_model"]


@pytest.mark.asyncio
async def test_admin_usage_filtering(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Test filtering options on /admin/usage."""
    db_key, plaintext_key = valid_api_key
    queue = get_usage_queue()

    headers = {"Authorization": f"Bearer {plaintext_key}"}
    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "mock-primary",
            "messages": [{"role": "user", "content": "Filter test"}],
        },
    )
    await queue.flush()

    admin_headers = {"X-Admin-Key": "test-admin-key"}

    # Filter by model
    res = await client.get("/admin/usage?model=mock-primary", headers=admin_headers)
    assert res.status_code == 200
    assert len(res.json()["logs"]) >= 1

    # Filter by non-existent model
    res_empty = await client.get("/admin/usage?model=nonexistent-model", headers=admin_headers)
    assert res_empty.status_code == 200
    assert len(res_empty.json()["logs"]) == 0

    # Filter by api_key_id
    res_key = await client.get(f"/admin/usage?api_key_id={db_key.id}", headers=admin_headers)
    assert res_key.status_code == 200
    assert len(res_key.json()["logs"]) >= 1
