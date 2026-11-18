"""Integration tests for retry policies, circuit breaker tripping, and fallback routing."""

import pytest
from httpx import AsyncClient

from llm_gateway.core.resilience.circuit_breaker import CircuitBreaker
from llm_gateway.db.models import ApiKey
from llm_gateway.providers.factory import get_provider_factory
from llm_gateway.providers.mock import MockProvider


@pytest.mark.asyncio
async def test_retry_then_succeed(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
):
    """Primary fails once with 500; retry mechanism catches it and succeeds on attempt 2."""
    factory = get_provider_factory()
    # Mock provider fails 1 time, then succeeds
    mock_prov = MockProvider(fail_n_times=1, fixed_text="Recovered on attempt 2")
    factory.register_provider("mock", mock_prov)

    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {"model": "mock-primary", "messages": [{"role": "user", "content": "test retry"}]}

    res = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert res.status_code == 200
    assert "Recovered on attempt 2" in res.json()["choices"][0]["message"]["content"]
    assert res.headers["X-Gateway-Fallback"] == "false"
    assert mock_prov.call_count == 2


@pytest.mark.asyncio
async def test_fallback_to_secondary_on_primary_exhaustion(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
):
    """When primary provider in alias chain permanently fails, fallback serves response with header."""
    factory = get_provider_factory()

    # Define a special fallback mock setup
    class DynamicMockProvider(MockProvider):
        async def complete(self, request):
            if request.model == "mock-primary":
                from llm_gateway.providers.base import ProviderServerError

                raise ProviderServerError("Primary target down", status_code=500)
            return await super().complete(request)

    dynamic_prov = DynamicMockProvider(fixed_text="Secondary fallback served successfully")
    factory.register_provider("mock", dynamic_prov)

    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}
    # "mock-chain" has primary: mock-primary, secondary: mock-secondary
    payload = {"model": "mock-chain", "messages": [{"role": "user", "content": "test fallback"}]}

    res = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "Secondary fallback" in data["choices"][0]["message"]["content"]
    # Verify fallback header
    assert res.headers["X-Gateway-Fallback"] == "true"
    assert res.headers["X-Gateway-Provider"] == "mock"


@pytest.mark.asyncio
async def test_circuit_breaker_open_skips_immediately(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
    fake_redis,
):
    """When primary circuit is OPEN, gateway immediately routes to fallback without trying primary."""
    factory = get_provider_factory()
    mock_prov = MockProvider(fixed_text="Fallback response from open circuit")
    factory.register_provider("mock", mock_prov)

    # Manually trip circuit breaker for mock:mock-primary
    cb = CircuitBreaker(
        fake_redis, provider="mock", model="mock-primary", failure_threshold=1, cooldown_seconds=60
    )
    await cb.record_failure()
    assert await cb.can_execute() is False

    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {"model": "mock-chain", "messages": [{"role": "user", "content": "test circuit"}]}

    res = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.headers["X-Gateway-Fallback"] == "true"
