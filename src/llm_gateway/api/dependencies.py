"""FastAPI dependency injection providers for authentication, Redis, and repositories."""

from collections.abc import AsyncGenerator
from decimal import Decimal

from fastapi import Depends, Header
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.core.auth import (
    constant_time_compare,
    get_cached_key_info,
    hash_api_key,
    set_cached_key_info,
)
from llm_gateway.core.cache import ResponseCache
from llm_gateway.core.rate_limit import SlidingWindowRateLimiter
from llm_gateway.db.models import ApiKey
from llm_gateway.db.repositories.api_key_repo import ApiKeyRepository
from llm_gateway.db.session import get_db_session
from llm_gateway.schemas.errors import GatewayException

_redis_pool: Redis | None = None


async def get_redis() -> AsyncGenerator[Redis, None]:
    """Yield a connected async Redis client instance."""
    global _redis_pool
    if _redis_pool is None:
        settings = get_settings()
        _redis_pool = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
    yield _redis_pool


async def get_api_key_repo(session: AsyncSession = Depends(get_db_session)) -> ApiKeyRepository:
    """Dependency yielding ApiKeyRepository."""
    return ApiKeyRepository(session)


async def get_rate_limiter(redis_client: Redis = Depends(get_redis)) -> SlidingWindowRateLimiter:
    """Dependency yielding SlidingWindowRateLimiter."""
    return SlidingWindowRateLimiter(redis_client)


async def get_response_cache(redis_client: Redis = Depends(get_redis)) -> ResponseCache:
    """Dependency yielding ResponseCache."""
    return ResponseCache(redis_client)


async def verify_admin_key(
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
    authorization: str | None = Header(None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Verify admin master key via X-Admin-Key header or Authorization: Bearer."""
    provided_key: str | None = x_admin_key
    if not provided_key and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided_key = parts[1]

    if not provided_key or not constant_time_compare(provided_key, settings.ADMIN_API_KEY):
        raise GatewayException(
            status_code=401,
            message="Invalid or missing admin API key.",
            error_type="authentication_error",
        )
    return provided_key


async def verify_client_api_key(
    authorization: str | None = Header(None, alias="Authorization"),
    repo: ApiKeyRepository = Depends(get_api_key_repo),
    redis_client: Redis = Depends(get_redis),
) -> ApiKey:
    """Authenticate client request via Bearer token with Redis caching and constant-time check."""
    if not authorization:
        raise GatewayException(
            status_code=401,
            message="Missing Authorization header. Expected 'Authorization: Bearer <key>'.",
            error_type="authentication_error",
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise GatewayException(
            status_code=401,
            message="Invalid Authorization format. Expected 'Bearer <key>'.",
            error_type="authentication_error",
        )

    raw_key = parts[1]
    key_hash = hash_api_key(raw_key)

    # 1. Fast-path lookup in Redis cache (60s TTL)
    cached_info = await get_cached_key_info(redis_client, key_hash)
    if cached_info:
        if cached_info.get("is_revoked"):
            raise GatewayException(
                status_code=403,
                message="This API key has been revoked.",
                error_type="forbidden",
            )
        # Construct ApiKey instance from cached metadata
        return ApiKey(
            id=cached_info["id"],
            name=cached_info["name"],
            key_hash=key_hash,
            key_prefix=cached_info["key_prefix"],
            requests_per_minute=cached_info["requests_per_minute"],
            monthly_budget_usd=Decimal(str(cached_info["monthly_budget_usd"])),
            is_revoked=False,
            isolate_cache=cached_info["isolate_cache"],
        )

    # 2. Database lookup
    db_key = await repo.get_by_hash(key_hash)
    if not db_key or not constant_time_compare(key_hash, db_key.key_hash):
        raise GatewayException(
            status_code=401,
            message="Invalid API key provided.",
            error_type="authentication_error",
        )

    if db_key.is_revoked:
        # Cache revoked status so subsequent calls don't hit Postgres
        await set_cached_key_info(
            redis_client,
            key_hash,
            {"id": db_key.id, "is_revoked": True},
            ttl=60,
        )
        raise GatewayException(
            status_code=403,
            message="This API key has been revoked.",
            error_type="forbidden",
        )

    # 3. Populate Redis cache with active key metadata
    await set_cached_key_info(
        redis_client,
        key_hash,
        {
            "id": db_key.id,
            "name": db_key.name,
            "key_prefix": db_key.key_prefix,
            "requests_per_minute": db_key.requests_per_minute,
            "monthly_budget_usd": str(db_key.monthly_budget_usd),
            "is_revoked": False,
            "isolate_cache": db_key.isolate_cache,
        },
        ttl=60,
    )

    return db_key
