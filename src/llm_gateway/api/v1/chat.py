"""OpenAI-compatible Chat Completions API endpoint with streaming, caching, and fallback."""

import time
import uuid

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from llm_gateway.api.dependencies import (
    get_rate_limiter,
    get_redis,
    get_response_cache,
    verify_client_api_key,
)
from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.core.cache import (
    ResponseCache,
    is_cache_eligible,
    normalize_request_for_cache,
    parse_cache_control,
)
from llm_gateway.core.rate_limit import SlidingWindowRateLimiter, check_monthly_budget
from llm_gateway.core.resilience.fallback import FallbackExecutor, FallbackResult
from llm_gateway.core.resilience.streaming import stream_chat_completions
from llm_gateway.core.routing import RequestRouter, get_router
from llm_gateway.db.models import ApiKey
from llm_gateway.db.session import get_db_session
from llm_gateway.observability.logging import request_id_ctx
from llm_gateway.providers.base import ProviderError, ProviderResponse
from llm_gateway.providers.factory import ProviderFactory, get_provider_factory
from llm_gateway.schemas.chat import (
    ChatChoice,
    ChatChoiceMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    UsageInfo,
)
from llm_gateway.schemas.errors import GatewayException

router = APIRouter(prefix="/v1", tags=["Chat"])


def _set_gateway_headers(
    response: Response,
    provider_name: str,
    cache_status: str = "MISS",
    fallback_used: bool = False,
    limit: int = 60,
    remaining: int = 59,
    reset_seconds: int = 60,
) -> None:
    """Attach standard gateway telemetry and rate limit headers."""
    req_id = request_id_ctx.get()
    response.headers["X-Request-ID"] = req_id
    response.headers["X-Gateway-Provider"] = provider_name
    response.headers["X-Gateway-Cache"] = cache_status
    response.headers["X-Gateway-Fallback"] = "true" if fallback_used else "false"
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(reset_seconds)


@router.post(
    "/chat/completions",
    status_code=status.HTTP_200_OK,
)
async def chat_completions(
    request: ChatCompletionRequest,
    response: Response,
    api_key: ApiKey = Depends(verify_client_api_key),
    rate_limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
    response_cache: ResponseCache = Depends(get_response_cache),
    redis_client: Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_db_session),
    router_engine: RequestRouter = Depends(get_router),
    factory: ProviderFactory = Depends(get_provider_factory),
    settings: Settings = Depends(get_settings),
    cache_control: str | None = Header(None, alias="Cache-Control"),
):
    """Handle OpenAI-style chat completions requests (both non-streaming and SSE streaming)."""
    # 1. Monthly budget check
    await check_monthly_budget(api_key, redis_client, session)

    # 2. Sliding window rate limiting
    rate_info = await rate_limiter.check_rate_limit(api_key)
    if not rate_info.allowed:
        headers = {
            "Retry-After": str(rate_info.retry_after),
            "X-RateLimit-Limit": str(rate_info.limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(rate_info.reset_seconds),
        }
        raise GatewayException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message=f"Rate limit of {rate_info.limit} RPM exceeded. Retry after {rate_info.retry_after} seconds.",
            error_type="rate_limit_error",
            code="rate_limit_exceeded",
            headers=headers,
        )

    # 3. Input validation limits
    if len(request.messages) > settings.MAX_REQUEST_MESSAGES:
        raise GatewayException(
            status_code=400,
            message=f"Request contains {len(request.messages)} messages, exceeding maximum of {settings.MAX_REQUEST_MESSAGES}",
            error_type="invalid_request_error",
            param="messages",
        )

    if request.max_tokens and request.max_tokens > settings.MAX_REQUEST_TOKENS_CEILING:
        raise GatewayException(
            status_code=400,
            message=f"max_tokens ({request.max_tokens}) exceeds maximum allowable ceiling of {settings.MAX_REQUEST_TOKENS_CEILING}",
            error_type="invalid_request_error",
            param="max_tokens",
        )

    # 4. Resolve routing alias or model into target chain
    try:
        targets = router_engine.resolve(request.model)
    except ValueError as exc:
        raise GatewayException(
            status_code=400,
            message=str(exc),
            error_type="invalid_request_error",
            param="model",
        ) from exc

    # 5. Handle Streaming Request (SSE)
    if request.stream:
        stream_metadata = {"provider": targets[0].provider, "fallback_used": False}
        stream_gen = stream_chat_completions(
            request=request,
            targets=targets,
            redis_client=redis_client,
            factory=factory,
            settings=settings,
            stream_metadata=stream_metadata,
        )

        stream_headers = {
            "X-Request-ID": request_id_ctx.get(),
            "X-Gateway-Provider": targets[0].provider,
            "X-Gateway-Cache": "MISS",
            "X-Gateway-Fallback": "false",
            "X-RateLimit-Limit": str(rate_info.limit),
            "X-RateLimit-Remaining": str(rate_info.remaining),
            "X-RateLimit-Reset": str(rate_info.reset_seconds),
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            stream_gen,
            media_type="text/event-stream",
            headers=stream_headers,
        )

    # 6. Non-Streaming path
    executor = FallbackExecutor(
        redis_client=redis_client,
        factory=factory,
        settings=settings,
    )

    fallback_metadata = {"fallback_used": False}

    async def compute_resilient_completion() -> ProviderResponse:
        try:
            fb_res: FallbackResult = await executor.execute(request, targets)
            fallback_metadata["fallback_used"] = fb_res.fallback_used
            return fb_res.response
        except ProviderError as err:
            raise GatewayException(
                status_code=err.status_code,
                message=err.message,
                error_type="upstream_error" if err.status_code >= 500 else "invalid_request_error",
            ) from err
        except Exception as exc:
            raise GatewayException(
                status_code=500,
                message=f"Internal error processing completion: {exc}",
                error_type="api_error",
            ) from exc

    # Check cache eligibility & execute
    no_cache, no_store = parse_cache_control(cache_control)
    cache_eligible = is_cache_eligible(request, settings)

    if cache_eligible:
        isolate = api_key.isolate_cache or settings.CACHE_ISOLATION_DEFAULT
        cache_key = normalize_request_for_cache(
            request,
            api_key=api_key,
            isolate_cache=isolate,
        )
        provider_resp, is_cache_hit = await response_cache.get_or_compute(
            cache_key=cache_key,
            compute_fn=compute_resilient_completion,
            no_cache=no_cache,
            no_store=no_store,
            ttl_seconds=settings.CACHE_TTL_SECONDS,
        )
        cache_status = "HIT" if is_cache_hit else "MISS"
    else:
        provider_resp = await compute_resilient_completion()
        cache_status = "MISS"

    _set_gateway_headers(
        response=response,
        provider_name=provider_resp.raw_provider_id,
        cache_status=cache_status,
        fallback_used=fallback_metadata["fallback_used"],
        limit=rate_info.limit,
        remaining=rate_info.remaining,
        reset_seconds=rate_info.reset_seconds,
    )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    return ChatCompletionResponse(
        id=completion_id,
        created=created_ts,
        model=request.model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatChoiceMessage(role="assistant", content=provider_resp.text),
                finish_reason=provider_resp.finish_reason,
            )
        ],
        usage=UsageInfo(
            prompt_tokens=provider_resp.input_tokens,
            completion_tokens=provider_resp.output_tokens,
            total_tokens=provider_resp.input_tokens + provider_resp.output_tokens,
        ),
    )
