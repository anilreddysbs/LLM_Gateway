"""Resilience orchestration engine: retries, circuit breaking, and alias fallback chains."""

import asyncio
import logging
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from llm_gateway.config.models_config import RoutingTarget
from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.core.resilience.backoff import compute_backoff_delay
from llm_gateway.core.resilience.circuit_breaker import CircuitBreaker
from llm_gateway.core.resilience.retry import get_retry_after, is_retriable_error
from llm_gateway.providers.base import (
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderTimeoutError,
)
from llm_gateway.providers.factory import ProviderFactory, get_provider_factory
from llm_gateway.schemas.chat import ChatCompletionRequest

logger = logging.getLogger("llm_gateway.resilience")


@dataclass
class FallbackResult:
    """Encapsulates the final response and telemetry regarding fallback routing."""

    response: ProviderResponse
    fallback_used: bool
    target_used: RoutingTarget
    attempts: int


class FallbackExecutor:
    """Executes requests across an ordered fallback chain with retries and circuit breaking."""

    def __init__(
        self,
        redis_client: Redis,
        factory: ProviderFactory | None = None,
        settings: Settings | None = None,
    ):
        self.redis = redis_client
        self.factory = factory or get_provider_factory()
        self.settings = settings or get_settings()

    async def execute(
        self,
        request: ChatCompletionRequest,
        targets: list[RoutingTarget],
    ) -> FallbackResult:
        """Traverse targets in order: try primary, retry on failures, or fallback to secondary."""
        start_time = time.monotonic()
        overall_deadline = start_time + self.settings.REQUEST_TIMEOUT_SECONDS

        last_error: Exception | None = None
        total_attempts = 0

        for target_idx, target in enumerate(targets):
            is_fallback = target_idx > 0
            provider = self.factory.get_provider(target.provider)
            cb = CircuitBreaker(
                redis_client=self.redis,
                provider=target.provider,
                model=target.model,
                failure_threshold=self.settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                cooldown_seconds=self.settings.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
            )

            # 1. Circuit Breaker Fast-Fail Check
            if not await cb.can_execute():
                logger.info(
                    f"Circuit OPEN for target {target.provider}:{target.model}. "
                    f"Skipping directly to next fallback target."
                )
                continue

            # 2. Attempt execution against current target with retries
            max_retries = self.settings.MAX_RETRIES

            for attempt in range(max_retries + 1):
                total_attempts += 1
                now = time.monotonic()
                if now >= overall_deadline:
                    raise ProviderTimeoutError(
                        f"Overall gateway request deadline ({self.settings.REQUEST_TIMEOUT_SECONDS}s) exceeded."
                    )

                # Remaining time budget for this specific attempt
                remaining_deadline = max(1.0, overall_deadline - now)
                per_attempt_timeout = min(
                    self.settings.PROVIDER_ATTEMPT_TIMEOUT_SECONDS, remaining_deadline
                )

                provider_req = ProviderRequest(
                    model=target.model,
                    messages=request.messages,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    timeout_seconds=per_attempt_timeout,
                )

                try:
                    logger.debug(
                        f"Calling target {target.provider}:{target.model} (attempt {attempt + 1}/{max_retries + 1})"
                    )
                    resp = await provider.complete(provider_req)

                    # Success: record in circuit breaker and return
                    await cb.record_success()
                    if is_fallback:
                        logger.info(
                            f"Request served by fallback target: {target.provider}:{target.model}"
                        )

                    return FallbackResult(
                        response=resp,
                        fallback_used=is_fallback,
                        target_used=target,
                        attempts=total_attempts,
                    )

                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        f"Target {target.provider}:{target.model} failed on attempt {attempt + 1}: {exc}"
                    )

                    # Non-retriable client errors (400, 401, etc.) fail-fast immediately
                    if not is_retriable_error(exc):
                        logger.info(
                            "Error is non-retriable client error; failing fast without retry."
                        )
                        raise

                    # If attempts remain for this target, backoff and retry
                    if attempt < max_retries:
                        retry_after = get_retry_after(exc)
                        delay = compute_backoff_delay(attempt, retry_after=retry_after)
                        logger.info(
                            f"Retrying target {target.provider}:{target.model} in {delay:.2f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        # Retries exhausted for this target: trip circuit breaker counter
                        await cb.record_failure()
                        logger.warning(
                            f"Exhausted {max_retries + 1} attempts on {target.provider}:{target.model}. "
                            f"Advancing to next target in alias chain."
                        )

        # All targets in chain failed
        if last_error:
            if isinstance(last_error, ProviderError):
                raise last_error
            raise ProviderError(
                f"All fallback targets exhausted. Last error: {last_error}", status_code=502
            )
        raise ProviderError(
            "No eligible provider targets available (all circuits open).", status_code=503
        )
