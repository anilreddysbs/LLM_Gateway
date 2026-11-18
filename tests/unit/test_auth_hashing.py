"""Unit tests for API key generation, SHA-256 hashing, constant-time compare, and Redis caching."""

import pytest
from redis.asyncio import Redis

from llm_gateway.core.auth import (
    constant_time_compare,
    generate_api_key,
    get_cached_key_info,
    hash_api_key,
    invalidate_key_cache,
    set_cached_key_info,
)


def test_generate_and_hash_key():
    """Verify generated keys have live prefix, 64-char hex SHA-256 hash, and correct masking."""
    raw_key, key_hash, key_prefix = generate_api_key()

    assert raw_key.startswith("gw-live-")
    assert len(key_hash) == 64
    assert int(key_hash, 16) > 0  # valid hex
    assert "..." in key_prefix
    assert key_prefix.startswith(raw_key[:10])

    # Re-hashing produces identical deterministic hash
    assert hash_api_key(raw_key) == key_hash


def test_constant_time_compare():
    """Verify constant-time equality and mismatch detection."""
    str1 = "gw-live-token-secret-123"
    str2 = "gw-live-token-secret-123"
    str3 = "gw-live-token-secret-999"

    assert constant_time_compare(str1, str2) is True
    assert constant_time_compare(str1, str3) is False
    assert constant_time_compare(str1, "") is False


@pytest.mark.asyncio
async def test_auth_redis_cache_operations(fake_redis: Redis):
    """Verify caching, reading, and invalidation of API key metadata in Redis."""
    key_hash = hash_api_key("gw-live-test-12345")
    key_info = {
        "id": "key-uuid-1",
        "name": "Test Key",
        "requests_per_minute": 120,
        "is_revoked": False,
    }

    # Cache MISS initially
    assert await get_cached_key_info(fake_redis, key_hash) is None

    # Populate cache
    await set_cached_key_info(fake_redis, key_hash, key_info, ttl=60)

    # Cache HIT
    cached = await get_cached_key_info(fake_redis, key_hash)
    assert cached is not None
    assert cached["id"] == "key-uuid-1"
    assert cached["requests_per_minute"] == 120

    # Invalidate cache
    await invalidate_key_cache(fake_redis, key_hash)
    assert await get_cached_key_info(fake_redis, key_hash) is None
