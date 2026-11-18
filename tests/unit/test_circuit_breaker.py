"""Unit tests for Circuit Breaker state transitions (CLOSED, OPEN, HALF_OPEN) and probes."""

import pytest
from redis.asyncio import Redis

from llm_gateway.core.resilience.circuit_breaker import CircuitBreaker, CircuitState


@pytest.mark.asyncio
async def test_circuit_breaker_transitions(fake_redis: Redis):
    """Verify CLOSED -> 5 failures -> OPEN -> cooldown -> HALF_OPEN -> success -> CLOSED."""
    cb = CircuitBreaker(
        redis_client=fake_redis,
        provider="mock",
        model="mock-unit-test",
        failure_threshold=3,
        cooldown_seconds=1,  # 1s for fast test
    )

    # 1. Starts CLOSED
    assert await cb.get_state() == CircuitState.CLOSED
    assert await cb.can_execute() is True

    # 2. Record 2 failures (below threshold) -> remains CLOSED
    await cb.record_failure()
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.CLOSED
    assert await cb.can_execute() is True

    # 3. 3rd failure reaches threshold -> trips to OPEN
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.OPEN
    assert await cb.can_execute() is False

    # 4. Wait out cooldown (1.1s)
    import asyncio

    await asyncio.sleep(1.1)

    # 5. Transitions to HALF_OPEN
    assert await cb.get_state() == CircuitState.HALF_OPEN
    # Allows 1 probe request
    assert await cb.can_execute() is True
    # Second probe rejected (exceeds max_probes=1)
    assert await cb.can_execute() is False

    # 6. Probe succeeds -> reset to CLOSED
    await cb.record_success()
    assert await cb.get_state() == CircuitState.CLOSED
    assert await cb.can_execute() is True


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_failure_re_trips(fake_redis: Redis):
    """If probe fails during HALF_OPEN, circuit immediately re-trips to OPEN."""
    cb = CircuitBreaker(
        redis_client=fake_redis,
        provider="mock",
        model="mock-probe-fail",
        failure_threshold=1,
        cooldown_seconds=1,
    )

    # Trip to OPEN
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.OPEN

    # Wait for HALF_OPEN
    import asyncio

    await asyncio.sleep(1.1)
    assert await cb.get_state() == CircuitState.HALF_OPEN

    # Probe fails -> immediately trips back to OPEN
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.OPEN
    assert await cb.can_execute() is False
