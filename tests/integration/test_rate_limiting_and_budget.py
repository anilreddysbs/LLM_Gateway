"""Integration tests for rate limiting (429) and monthly budget exhaustion (402)."""

from datetime import UTC
from decimal import Decimal

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from llm_gateway.core.rate_limit import get_budget_cache_key
from llm_gateway.db.repositories.api_key_repo import ApiKeyRepository


@pytest.mark.asyncio
async def test_rate_limiting_enforcement_and_headers(
    client: AsyncClient,
    test_db_session: AsyncSession,
):
    """Exceeding requests_per_minute returns 429 Too Many Requests and required headers."""
    # Create key with limit = 2 RPM
    from llm_gateway.core.auth import generate_api_key

    raw_key, key_hash, key_prefix = generate_api_key()
    repo = ApiKeyRepository(test_db_session)
    await repo.create(
        name="Low RPM Key",
        key_hash=key_hash,
        key_prefix=key_prefix,
        requests_per_minute=2,
    )

    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {"model": "mock-primary", "messages": [{"role": "user", "content": "hi"}]}

    # Request 1: OK
    res1 = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert res1.status_code == 200
    assert res1.headers["X-RateLimit-Limit"] == "2"
    assert res1.headers["X-RateLimit-Remaining"] == "1"

    # Request 2: OK
    res2 = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.headers["X-RateLimit-Remaining"] == "0"

    # Request 3: 429 Too Many Requests
    res3 = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert res3.status_code == 429
    assert res3.headers["X-RateLimit-Remaining"] == "0"
    assert "Retry-After" in res3.headers
    assert int(res3.headers["Retry-After"]) >= 1

    error_body = res3.json()["error"]
    assert error_body["type"] == "rate_limit_error"
    assert error_body["code"] == "rate_limit_exceeded"
    assert "Retry after" in error_body["message"]


@pytest.mark.asyncio
async def test_monthly_budget_enforcement(
    client: AsyncClient,
    test_db_session: AsyncSession,
    fake_redis: Redis,
):
    """When current monthly spend exceeds monthly_budget_usd, request returns 402."""
    from datetime import datetime

    from llm_gateway.core.auth import generate_api_key

    raw_key, key_hash, key_prefix = generate_api_key()
    repo = ApiKeyRepository(test_db_session)
    db_key = await repo.create(
        name="Budget Test Key",
        key_hash=key_hash,
        key_prefix=key_prefix,
        requests_per_minute=60,
        monthly_budget_usd=Decimal("5.0000"),
    )

    # Simulate $6.50 current spend in Redis
    now = datetime.now(UTC)
    year_month = now.strftime("%Y-%m")
    cache_key = get_budget_cache_key(db_key.id, year_month)
    await fake_redis.set(cache_key, "6.5000")

    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {"model": "mock-primary", "messages": [{"role": "user", "content": "hi"}]}

    response = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert response.status_code == 402

    error_body = response.json()["error"]
    assert error_body["type"] == "insufficient_quota"
    assert error_body["code"] == "budget_exceeded"
    assert "Monthly budget of $5.00 exceeded" in error_body["message"]
    assert "6.5000" in error_body["message"]
