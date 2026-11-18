"""Cryptographic authentication utilities, key generation, and Redis lookup caching."""

import contextlib
import hashlib
import hmac
import json
import secrets
from typing import Any

from redis.asyncio import Redis

KEY_PREFIX_LENGTH = 10
AUTH_CACHE_TTL_SECONDS = 60


def generate_api_key() -> tuple[str, str, str]:
    """Generate a secure cryptographically random API key, its SHA-256 hash, and display prefix.

    Returns:
        tuple[raw_key, key_hash, key_prefix]
    """
    token = secrets.token_urlsafe(32)
    raw_key = f"gw-live-{token}"
    key_hash = hash_api_key(raw_key)
    # Masked prefix for admin visibility: e.g. gw-live-abc...1234
    key_prefix = f"{raw_key[:12]}...{raw_key[-4:]}"
    return raw_key, key_hash, key_prefix


def hash_api_key(raw_key: str) -> str:
    """Compute deterministic SHA-256 hex digest of an API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def constant_time_compare(val_a: str, val_b: str) -> bool:
    """Constant-time comparison mitigating timing side-channel attacks."""
    return hmac.compare_digest(val_a, val_b)


def get_auth_cache_key(key_hash: str) -> str:
    """Redis key storing cached authenticated API key metadata."""
    return f"llm_gateway:auth:{key_hash}"


async def get_cached_key_info(redis_client: Redis, key_hash: str) -> dict[str, Any] | None:
    """Retrieve cached key record from Redis if present."""
    cache_key = get_auth_cache_key(key_hash)
    try:
        raw_val = await redis_client.get(cache_key)
        if raw_val:
            return json.loads(raw_val)
    except Exception:
        # Cache failures must never block request authentication
        pass
    return None


async def set_cached_key_info(
    redis_client: Redis,
    key_hash: str,
    key_info: dict[str, Any],
    ttl: int = AUTH_CACHE_TTL_SECONDS,
) -> None:
    """Store serialized key record in Redis with a 60-second TTL."""
    cache_key = get_auth_cache_key(key_hash)
    with contextlib.suppress(Exception):
        await redis_client.set(cache_key, json.dumps(key_info), ex=ttl)


async def invalidate_key_cache(redis_client: Redis, key_hash: str) -> None:
    """Evict cached API key upon revocation or modification."""
    cache_key = get_auth_cache_key(key_hash)
    with contextlib.suppress(Exception):
        await redis_client.delete(cache_key)
