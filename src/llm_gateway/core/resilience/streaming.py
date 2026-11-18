"""Streaming response generator with the fallback-before-first-chunk guarantee."""

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

from redis.asyncio import Redis

from llm_gateway.config.models_config import ModelsConfig, RoutingTarget
from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.core.resilience.circuit_breaker import CircuitBreaker
from llm_gateway.core.resilience.retry import is_retriable_error
from llm_gateway.observability.background import UsageLogEntry, UsageLogQueue
from llm_gateway.observability.logging import request_id_ctx
from llm_gateway.providers.base import ProviderRequest
from llm_gateway.providers.factory import ProviderFactory, get_provider_factory
from llm_gateway.schemas.chat import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionRequest,
    UsageInfo,
)

logger = logging.getLogger("llm_gateway.streaming")


def format_sse(data: str) -> str:
    """Format string as standard Server-Sent Event."""
    return f"data: {data}\n\n"


async def stream_chat_completions(
    request: ChatCompletionRequest,
    targets: list[RoutingTarget],
    redis_client: Redis,
    factory: ProviderFactory | None = None,
    settings: Settings | None = None,
    stream_metadata: dict | None = None,
    api_key_id: str | None = None,
    router_config: ModelsConfig | None = None,
    usage_queue: UsageLogQueue | None = None,
) -> AsyncIterator[str]:
    """Execute streaming completion across target chain with fallback-before-first-chunk rule.

    CRITICAL INVARIANT:
    - Fallback is strictly allowed BEFORE the first chunk has been transmitted over the wire.
    - Once the first chunk is sent, the client has received HTTP 200 and initial output.
      Any subsequent error MUST emit an SSE error event and close the stream.
    """
    factory = factory or get_provider_factory()
    settings = settings or get_settings()
    stream_metadata = stream_metadata if stream_metadata is not None else {}

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())
    start_time = time.perf_counter()

    first_chunk_sent = False
    active_cb: CircuitBreaker | None = None

    for target_idx, target in enumerate(targets):
        is_fallback = target_idx > 0
        provider = factory.get_provider(target.provider)
        cb = CircuitBreaker(
            redis_client=redis_client,
            provider=target.provider,
            model=target.model,
            failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            cooldown_seconds=settings.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )

        if not await cb.can_execute():
            logger.info(
                f"Circuit OPEN for streaming target {target.provider}:{target.model}; skipping."
            )
            continue

        active_cb = cb
        stream_metadata["provider"] = target.provider
        stream_metadata["fallback_used"] = is_fallback

        provider_req = ProviderRequest(
            model=target.model,
            messages=request.messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            timeout_seconds=settings.PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
        )

        total_output_chars = 0
        last_input_tokens = 0
        last_output_tokens = 0

        try:
            stream_iter = provider.stream(provider_req)

            # Send initial chunk setting role="assistant"
            role_chunk = ChatCompletionChunk(
                id=completion_id,
                created=created_ts,
                model=request.model,
                choices=[
                    ChatCompletionChunkChoice(
                        index=0,
                        delta=ChatCompletionChunkDelta(role="assistant", content=""),
                        finish_reason=None,
                    )
                ],
            )

            # Peek / wait for the very first chunk from upstream
            # If this raises, we catch it BEFORE sending anything to the client!
            async for chunk in stream_iter:
                if not first_chunk_sent:
                    # Successfully received first chunk from upstream!
                    # Now safe to flush initial role chunk to client
                    first_chunk_sent = True
                    yield format_sse(role_chunk.model_dump_json(exclude_none=True))

                if chunk.text:
                    total_output_chars += len(chunk.text)
                if chunk.input_tokens:
                    last_input_tokens = chunk.input_tokens
                if chunk.output_tokens:
                    last_output_tokens = chunk.output_tokens

                content_chunk = ChatCompletionChunk(
                    id=completion_id,
                    created=created_ts,
                    model=request.model,
                    choices=[
                        ChatCompletionChunkChoice(
                            index=0,
                            delta=ChatCompletionChunkDelta(content=chunk.text),
                            finish_reason=chunk.finish_reason,
                        )
                    ],
                    usage=(
                        UsageInfo(
                            prompt_tokens=chunk.input_tokens or 0,
                            completion_tokens=chunk.output_tokens or 0,
                            total_tokens=(chunk.input_tokens or 0) + (chunk.output_tokens or 0),
                        )
                        if chunk.finish_reason and (chunk.input_tokens or chunk.output_tokens)
                        else None
                    ),
                )
                yield format_sse(content_chunk.model_dump_json(exclude_none=True))

            # Stream finished cleanly
            if active_cb:
                await active_cb.record_success()

            if usage_queue:
                latency_ms = (time.perf_counter() - start_time) * 1000
                estimated = False
                in_tokens = last_input_tokens
                out_tokens = last_output_tokens
                if in_tokens == 0 and out_tokens == 0:
                    estimated = True
                    in_tokens = max(1, sum(len(m.content) for m in request.messages) // 4)
                    out_tokens = max(1, total_output_chars // 4)

                cost_usd = Decimal("0.0")
                if router_config:
                    cost_usd = router_config.calculate_cost(target.model, in_tokens, out_tokens)

                usage_queue.enqueue(
                    UsageLogEntry(
                        request_id=request_id_ctx.get(),
                        api_key_id=api_key_id,
                        requested_model=request.model,
                        provider=target.provider,
                        model=target.model,
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        cost_usd=cost_usd,
                        latency_ms=round(latency_ms, 2),
                        cache_hit=False,
                        fallback_used=is_fallback,
                        attempts=target_idx + 1,
                        status_code=200,
                        alias=(
                            request.model
                            if router_config and request.model in router_config.aliases
                            else None
                        ),
                        streaming=True,
                        estimated_tokens=estimated,
                    )
                )

            yield format_sse("[DONE]")
            return

        except Exception as exc:
            logger.warning(f"Streaming error on target {target.provider}:{target.model}: {exc}")

            if first_chunk_sent:
                # VIOLATION PREVENTION:
                # Streaming has already begun! We CANNOT fallback to another provider.
                # Emit an SSE error chunk and terminate stream.
                if active_cb:
                    await active_cb.record_failure()

                if usage_queue:
                    latency_ms = (time.perf_counter() - start_time) * 1000
                    usage_queue.enqueue(
                        UsageLogEntry(
                            request_id=request_id_ctx.get(),
                            api_key_id=api_key_id,
                            requested_model=request.model,
                            provider=target.provider,
                            model=target.model,
                            input_tokens=0,
                            output_tokens=0,
                            cost_usd=Decimal("0.0"),
                            latency_ms=round(latency_ms, 2),
                            cache_hit=False,
                            fallback_used=is_fallback,
                            attempts=target_idx + 1,
                            status_code=500,
                            error_type="upstream_error",
                            alias=(
                                request.model
                                if router_config and request.model in router_config.aliases
                                else None
                            ),
                            streaming=True,
                        )
                    )

                error_payload = {
                    "error": {
                        "message": f"Streaming interrupted mid-flight: {exc}",
                        "type": "upstream_error",
                        "code": "stream_interrupted",
                    }
                }
                yield format_sse(json.dumps(error_payload))
                yield format_sse("[DONE]")
                return

            # If first chunk was NOT sent yet, fallback is safe!
            if active_cb and is_retriable_error(exc):
                await active_cb.record_failure()
            logger.info(
                f"Target {target.provider}:{target.model} failed before first chunk; attempting fallback."
            )
            continue

    # If loop exits without yielding, all targets failed before first chunk
    if not first_chunk_sent:
        if usage_queue:
            latency_ms = (time.perf_counter() - start_time) * 1000
            usage_queue.enqueue(
                UsageLogEntry(
                    request_id=request_id_ctx.get(),
                    api_key_id=api_key_id,
                    requested_model=request.model,
                    provider=targets[0].provider,
                    model=targets[0].model,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=Decimal("0.0"),
                    latency_ms=round(latency_ms, 2),
                    cache_hit=False,
                    fallback_used=True,
                    attempts=len(targets),
                    status_code=502,
                    error_type="upstream_error",
                    alias=(
                        request.model
                        if router_config and request.model in router_config.aliases
                        else None
                    ),
                    streaming=True,
                )
            )

        error_payload = {
            "error": {
                "message": "All fallback targets failed to initialize stream.",
                "type": "upstream_error",
                "code": "all_fallbacks_failed",
            }
        }
        yield format_sse(json.dumps(error_payload))
        yield format_sse("[DONE]")
