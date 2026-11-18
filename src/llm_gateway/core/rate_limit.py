"""Redis-backed Sliding Window Rate Limiter (atomic Lua) and Monthly Budget Enforcement."""

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.db.models import ApiKey, UsageLog
from llm_gateway.schemas.errors import GatewayException

logger = logging.getLogger("llm_gateway.rate_limit")

# Atomic sliding window rate limiter Lua script
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local req_id = ARGV[4]
local clear_before = now - window

-- 1. Evict entries outside the sliding window
redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)

-- 2. Check current request volume in window
local current_requests = redis.call('ZCARD', key)

if current_requests < limit then
    -- Allowed: record current request timestamp and refresh expiration
    redis.call('ZADD', key, now, req_id)
    redis.call('PEXPIRE', key, window)
    local remaining = limit - current_requests - 1
    return {1, remaining, 0}
else
    -- Exceeded: calculate exact retry-after based on oldest entry in window
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = 1
    if oldest and #oldest >= 2 then
        local oldest_time = tonumber(oldest[2])
        local time_to_free = math.ceil(((oldest_time + window) - now) / 1000)
        if time_to_free > 0 then
            retry_after = time_to_free
        end
    end
    return {0, 0, retry_after}
end
"""


@dataclass
class RateLimitResult:
    """Outcome of rate limit evaluation."""

    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after: int


class SlidingWindowRateLimiter:
    """Enforces per-API-key requests-per-minute limits atomically via Redis."""

    def __init__(self, redis_client: Redis, settings: Settings | None = None):
        self.redis = redis_client
        self.settings = settings or get_settings()

    def _get_key(self, api_key_id: str) -> str:
        return f"llm_gateway:ratelimit:{api_key_id}"

    async def check_rate_limit(self, api_key: ApiKey) -> RateLimitResult:
        """Evaluate sliding window limit. Supports configurable fail-open / fail-closed behavior."""
        limit = api_key.requests_per_minute
        window_ms = 60 * 1000  # 1 minute sliding window
        now_ms = int(time.time() * 1000)
        req_id = f"{now_ms}_{uuid.uuid4().hex[:8]}"
        key = self._get_key(api_key.id)

        try:
            res = await self.redis.eval(
                SLIDING_WINDOW_LUA,
                1,
                key,
                now_ms,
                window_ms,
                limit,
                req_id,
            )
            allowed = bool(res[0])
            remaining = int(res[1])
            retry_after = int(res[2])
            reset_seconds = retry_after if retry_after > 0 else 60

            return RateLimitResult(
                allowed=allowed,
                limit=limit,
                remaining=remaining,
                reset_seconds=reset_seconds,
                retry_after=retry_after,
            )
        except Exception as exc:
            logger.warning(
                f"Redis rate limiter failed: {exc}. Fail-open={self.settings.FAIL_OPEN_ON_REDIS_ERROR}"
            )
            if self.settings.FAIL_OPEN_ON_REDIS_ERROR:
                # Fail-open: allow traffic through during Redis downtime
                return RateLimitResult(
                    allowed=True,
                    limit=limit,
                    remaining=limit - 1,
                    reset_seconds=60,
                    retry_after=0,
                )
            # Fail-closed: protect upstream providers by returning 503
            raise GatewayException(
                status_code=503,
                message="Rate limiting service is temporarily unavailable. Fail-closed is active.",
                error_type="service_unavailable",
            ) from exc


def get_budget_cache_key(api_key_id: str, year_month: str) -> str:
    """Redis key storing aggregated USD spend for the current month."""
    return f"llm_gateway:budget:{api_key_id}:{year_month}"


async def check_monthly_budget(
    api_key: ApiKey,
    redis_client: Redis,
    session: AsyncSession,
) -> None:
    """Enforce per-key monthly USD budget. Cached in Redis to prevent heavy DB aggregation."""
    now = datetime.now(UTC)
    year_month = now.strftime("%Y-%m")
    cache_key = get_budget_cache_key(api_key.id, year_month)

    current_spend = Decimal("0.0")

    # 1. Check Redis cache
    try:
        cached_spend = await redis_client.get(cache_key)
        if cached_spend is not None:
            current_spend = Decimal(str(cached_spend))
        else:
            # 2. Cache miss: aggregate spend from usage_logs table
            start_of_month = datetime(now.year, now.month, 1, tzinfo=UTC)
            stmt = select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
                UsageLog.api_key_id == api_key.id,
                UsageLog.created_at >= start_of_month,
            )
            result = await session.execute(stmt)
            raw_sum = result.scalar_one()
            current_spend = Decimal(str(raw_sum))
            # Cache in Redis with 5 minute TTL (invalidated/updated on writes)
            await redis_client.set(cache_key, str(current_spend), ex=300)
    except Exception as exc:
        logger.warning(f"Error checking monthly budget in Redis: {exc}")

    # Check budget ceiling
    if current_spend >= api_key.monthly_budget_usd:
        raise GatewayException(
            status_code=402,
            message=(
                f"Monthly budget of ${api_key.monthly_budget_usd:.2f} exceeded. "
                f"Current spend for {year_month} is ${current_spend:.4f}."
            ),
            error_type="insufficient_quota",
            code="budget_exceeded",
        )
