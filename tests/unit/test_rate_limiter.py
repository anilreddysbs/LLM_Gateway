"""Unit tests for Sliding Window Rate Limiter and Redis Lua script."""

from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis

from llm_gateway.config.settings import Settings
from llm_gateway.core.rate_limit import SlidingWindowRateLimiter
from llm_gateway.db.models import ApiKey
from llm_gateway.schemas.errors import GatewayException


@pytest.mark.asyncio
async def test_sliding_window_allow_and_reject(fake_redis: Redis):
    """Rate limiter allows requests within RPM limit and rejects with positive retry_after upon overflow."""
    api_key = ApiKey(
        id="test-key-1",
        name="Test",
        key_hash="hash1",
        key_prefix="gw-live...",
        requests_per_minute=3,
    )
    limiter = SlidingWindowRateLimiter(fake_redis)

    # First 3 requests should be allowed
    res1 = await limiter.check_rate_limit(api_key)
    assert res1.allowed is True
    assert res1.remaining == 2

    res2 = await limiter.check_rate_limit(api_key)
    assert res2.allowed is True
    assert res2.remaining == 1

    res3 = await limiter.check_rate_limit(api_key)
    assert res3.allowed is True
    assert res3.remaining == 0

    # 4th request must be rejected
    res4 = await limiter.check_rate_limit(api_key)
    assert res4.allowed is False
    assert res4.remaining == 0
    assert res4.retry_after >= 1


@pytest.mark.asyncio
async def test_sliding_window_fail_open_behavior():
    """When Redis is down and FAIL_OPEN_ON_REDIS_ERROR is True, requests must be allowed."""
    mock_redis = AsyncMock(spec=Redis)
    mock_redis.register_script = lambda script: AsyncMock(
        side_effect=Exception("Redis connection refused")
    )

    settings = Settings(FAIL_OPEN_ON_REDIS_ERROR=True)
    limiter = SlidingWindowRateLimiter(mock_redis, settings=settings)

    api_key = ApiKey(
        id="test-key-failopen", name="Test", key_hash="h", key_prefix="p", requests_per_minute=10
    )
    result = await limiter.check_rate_limit(api_key)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_sliding_window_fail_closed_behavior():
    """When Redis is down and FAIL_OPEN_ON_REDIS_ERROR is False, request must raise 503."""
    mock_redis = AsyncMock(spec=Redis)
    mock_redis.register_script = lambda script: AsyncMock(
        side_effect=Exception("Redis connection refused")
    )

    settings = Settings(FAIL_OPEN_ON_REDIS_ERROR=False)
    limiter = SlidingWindowRateLimiter(mock_redis, settings=settings)

    api_key = ApiKey(
        id="test-key-failclosed", name="Test", key_hash="h", key_prefix="p", requests_per_minute=10
    )
    with pytest.raises(GatewayException) as exc_info:
        await limiter.check_rate_limit(api_key)
    assert exc_info.value.status_code == 503
