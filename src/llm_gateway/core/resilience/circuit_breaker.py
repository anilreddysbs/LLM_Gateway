"""Redis-backed distributed Circuit Breaker (CLOSED, OPEN, HALF_OPEN states)."""

import logging
import time
from enum import StrEnum

from redis.asyncio import Redis

logger = logging.getLogger("llm_gateway.circuit_breaker")


class CircuitState(StrEnum):
    """The operational state of a circuit breaker."""

    CLOSED = "CLOSED"  # Normal operation: all requests pass through
    OPEN = "OPEN"  # Tripped: all requests fail-fast / skip to fallback
    HALF_OPEN = "HALF_OPEN"  # Recovery probe: limited traffic tests upstream health


class CircuitBreaker:
    """Tracks upstream target availability in Redis to isolate cascading failures."""

    def __init__(
        self,
        redis_client: Redis,
        provider: str,
        model: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 30,
        max_half_open_probes: int = 1,
    ):
        self.redis = redis_client
        self.provider = provider
        self.model = model
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_half_open_probes = max_half_open_probes
        self._base_key = f"llm_gateway:cb:{provider}:{model}"

    @property
    def key_failures(self) -> str:
        return f"{self._base_key}:failures"

    @property
    def key_open_until(self) -> str:
        return f"{self._base_key}:open_until"

    @property
    def key_probes(self) -> str:
        return f"{self._base_key}:probes"

    async def get_state(self) -> CircuitState:
        """Inspect current circuit breaker state based on Redis keys and cooldown timestamps."""
        try:
            open_until_raw = await self.redis.get(self.key_open_until)
            if open_until_raw is not None:
                open_until = float(open_until_raw)
                now = time.time()
                if now < open_until:
                    return CircuitState.OPEN
                # Cooldown period elapsed -> transition to HALF_OPEN
                return CircuitState.HALF_OPEN
        except Exception as exc:
            logger.warning(
                f"Error checking circuit state in Redis for {self.provider}:{self.model}: {exc}"
            )
        return CircuitState.CLOSED

    async def can_execute(self) -> bool:
        """Evaluate if an execution attempt is permitted to this provider+model target."""
        state = await self.get_state()
        if state == CircuitState.CLOSED:
            return True

        if state == CircuitState.OPEN:
            logger.info(
                f"Circuit breaker OPEN for {self.provider}:{self.model}; skipping to fallback."
            )
            return False

        if state == CircuitState.HALF_OPEN:
            # Allow limited probe requests through
            try:
                probe_count = await self.redis.incr(self.key_probes)
                if probe_count <= self.max_half_open_probes:
                    logger.info(
                        f"Circuit breaker HALF_OPEN probe #{probe_count} allowed for {self.provider}:{self.model}"
                    )
                    return True
                return False
            except Exception:
                return True
        return True

    async def record_success(self) -> None:
        """Record a successful response, resetting failures and closing half-open circuits."""
        try:
            pipe = self.redis.pipeline()
            pipe.delete(self.key_failures)
            pipe.delete(self.key_open_until)
            pipe.delete(self.key_probes)
            await pipe.execute()
        except Exception as exc:
            logger.warning(f"Failed to record circuit breaker success: {exc}")

    async def record_failure(self) -> None:
        """Record a target failure. If failure threshold is breached, trip circuit to OPEN."""
        now = time.time()
        try:
            state = await self.get_state()
            if state == CircuitState.HALF_OPEN:
                # Probe failed: immediately trip back to OPEN for full cooldown
                pipe = self.redis.pipeline()
                pipe.set(
                    self.key_open_until,
                    str(now + self.cooldown_seconds),
                    ex=self.cooldown_seconds * 2,
                )
                pipe.delete(self.key_probes)
                await pipe.execute()
                logger.warning(
                    f"Probe failed: Circuit tripped back to OPEN for {self.provider}:{self.model}"
                )
                return

            # CLOSED state: increment failure counter
            failures = await self.redis.incr(self.key_failures)
            # Retain failure counter for 10 minutes if no further errors
            await self.redis.expire(self.key_failures, 600)

            if failures >= self.failure_threshold:
                # Trip to OPEN
                open_until = now + self.cooldown_seconds
                pipe = self.redis.pipeline()
                pipe.set(self.key_open_until, str(open_until), ex=self.cooldown_seconds * 2)
                pipe.delete(self.key_probes)
                await pipe.execute()
                logger.warning(
                    f"Circuit breaker TRIPPED to OPEN for {self.provider}:{self.model} "
                    f"after {failures} consecutive failures. Cooldown: {self.cooldown_seconds}s."
                )
        except Exception as exc:
            logger.warning(f"Failed to record circuit breaker failure: {exc}")
