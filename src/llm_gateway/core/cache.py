"""Exact-match Redis response caching with stampede protection (single-flight distributed lock)."""

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from redis.asyncio import Redis

from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.db.models import ApiKey
from llm_gateway.providers.base import ProviderResponse
from llm_gateway.schemas.chat import ChatCompletionRequest

logger = logging.getLogger("llm_gateway.cache")


def normalize_request_for_cache(
    request: ChatCompletionRequest,
    api_key: ApiKey | None = None,
    isolate_cache: bool = False,
    version_prefix: str = "v1",
) -> str:
    """Compute a canonical SHA-256 cache key for an exact-match chat completion request.

    By default, cache entries are shared across all tenants. If isolate_cache=True,
    the API key's ID is bound into the hash for tenant-isolated caching.
    """
    # Canonical message structure
    normalized_messages = [{"role": m.role, "content": m.content.strip()} for m in request.messages]

    canonical_payload: dict[str, Any] = {
        "version": version_prefix,
        "model": request.model.lower().strip(),
        "messages": normalized_messages,
        "temperature": round(request.temperature, 4) if request.temperature is not None else 1.0,
        "max_tokens": request.max_tokens,
    }

    if isolate_cache and api_key:
        canonical_payload["api_key_id"] = api_key.id

    serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    key_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"llm_gateway:cache:{version_prefix}:{key_hash}"


def parse_cache_control(cache_control: str | None) -> tuple[bool, bool]:
    """Parse Cache-Control header into (no_cache, no_store) boolean tuple.

    - no-cache: bypass cache lookup (force fresh upstream call).
    - no-store: skip writing result to cache.
    """
    if not cache_control:
        return False, False

    directives = [d.strip().lower() for d in cache_control.split(",")]
    no_cache = "no-cache" in directives
    no_store = "no-store" in directives
    return no_cache, no_store


def is_cache_eligible(
    request: ChatCompletionRequest,
    settings: Settings | None = None,
) -> bool:
    """Determine whether request qualifies for caching (non-streaming, low temperature)."""
    settings = settings or get_settings()
    if not settings.CACHE_ENABLED:
        return False

    # Streaming requests are not cached
    if request.stream:
        return False

    # High temperature introduces stochastic variance and should not be cached
    temp = request.temperature if request.temperature is not None else 1.0
    return temp <= settings.CACHE_MAX_TEMPERATURE


class ResponseCache:
    """Redis cache manager providing exact-match caching and stampede prevention."""

    def __init__(self, redis_client: Redis, settings: Settings | None = None):
        self.redis = redis_client
        self.settings = settings or get_settings()

    async def get(self, cache_key: str) -> ProviderResponse | None:
        """Fetch and deserialize a cached completion response."""
        try:
            raw = await self.redis.get(cache_key)
            if raw:
                data = json.loads(raw)
                return ProviderResponse(**data)
        except Exception as exc:
            logger.warning(f"Failed to read from cache key {cache_key}: {exc}")
        return None

    async def set(
        self,
        cache_key: str,
        response: ProviderResponse,
        ttl_seconds: int | None = None,
    ) -> None:
        """Serialize and persist response in Redis with TTL."""
        ttl = ttl_seconds if ttl_seconds is not None else self.settings.CACHE_TTL_SECONDS
        try:
            serialized = json.dumps(response.model_dump())
            await self.redis.set(cache_key, serialized, ex=ttl)
        except Exception as exc:
            logger.warning(f"Failed to write to cache key {cache_key}: {exc}")

    async def get_or_compute(
        self,
        cache_key: str,
        compute_fn: Callable[[], Coroutine[Any, Any, ProviderResponse]],
        no_cache: bool = False,
        no_store: bool = False,
        ttl_seconds: int | None = None,
        lock_timeout_seconds: int = 15,
    ) -> tuple[ProviderResponse, bool]:
        """Fetch cached response or compute with distributed stampede protection.

        Returns:
            tuple[ProviderResponse, is_cache_hit: bool]
        """
        # 1. Check cache if reading is allowed
        if not no_cache:
            cached_resp = await self.get(cache_key)
            if cached_resp is not None:
                return cached_resp, True

        # 2. Cache stampede protection: acquire distributed lock for this cache key
        lock_key = f"{cache_key}:lock"
        acquired = False
        with contextlib.suppress(Exception):
            acquired = await self.redis.set(lock_key, "1", nx=True, ex=lock_timeout_seconds)

        if acquired:
            try:
                # Double-check cache in case earlier lock holder completed right before we acquired
                if not no_cache:
                    double_check = await self.get(cache_key)
                    if double_check is not None:
                        return double_check, True

                # Compute the response via upstream provider
                computed_response = await compute_fn()

                # Write to cache if storing is permitted
                if not no_store:
                    await self.set(cache_key, computed_response, ttl_seconds=ttl_seconds)

                return computed_response, False
            finally:
                # Release distributed lock
                with contextlib.suppress(Exception):
                    await self.redis.delete(lock_key)
        else:
            # Another request is computing this response: wait and poll for the cached result
            poll_interval = 0.05
            max_polls = int(lock_timeout_seconds / poll_interval)

            for _ in range(max_polls):
                await asyncio.sleep(poll_interval)
                cached = await self.get(cache_key)
                if cached is not None:
                    return cached, True

            # If waiting timed out, compute directly as fallback
            logger.info(f"Stampede wait timed out for {cache_key}; falling back to compute.")
            fallback_response = await compute_fn()
            if not no_store:
                await self.set(cache_key, fallback_response, ttl_seconds=ttl_seconds)
            return fallback_response, False
