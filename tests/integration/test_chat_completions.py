"""Integration tests for POST /v1/chat/completions and GET /v1/models."""

import pytest
from httpx import AsyncClient

from llm_gateway.db.models import ApiKey
from llm_gateway.providers.factory import get_provider_factory
from llm_gateway.providers.mock import MockProvider


@pytest.fixture(autouse=True)
def setup_mock_providers():
    """Ensure mock provider is installed for test executions."""
    factory = get_provider_factory()
    mock_prov = MockProvider(fixed_text="Mock completion for {model}")
    factory.register_provider("mock", mock_prov)
    yield
    mock_prov.reset()


@pytest.mark.asyncio
async def test_chat_completions_mock_direct_model(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Calling chat completions with direct mock model returns valid OpenAI response and headers."""
    _, raw_key = valid_api_key
    payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "Hello, world!"}],
        "temperature": 0.7,
        "max_tokens": 100,
    }
    response = await client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "mock-primary"
    assert len(data["choices"]) == 1
    assert "Mock completion" in data["choices"][0]["message"]["content"]
    assert data["usage"]["prompt_tokens"] > 0
    assert data["usage"]["completion_tokens"] > 0

    # Verify Gateway headers
    assert "X-Request-ID" in response.headers
    assert response.headers["X-Gateway-Provider"] == "mock"
    assert response.headers["X-Gateway-Cache"] == "MISS"
    assert response.headers["X-Gateway-Fallback"] == "false"
    assert "X-RateLimit-Limit" in response.headers


@pytest.mark.asyncio
async def test_chat_completions_mock_alias_routing(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Calling chat completions with configured alias 'mock-chain' routes to primary target."""
    _, raw_key = valid_api_key
    payload = {
        "model": "mock-chain",
        "messages": [
            {"role": "system", "content": "You are a test assistant"},
            {"role": "user", "content": "Testing alias routing"},
        ],
    }
    response = await client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["model"] == "mock-chain"
    assert response.headers["X-Gateway-Provider"] == "mock"


@pytest.mark.asyncio
async def test_chat_completions_invalid_model(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Unknown model or alias should return 400 Bad Request with OpenAI error format."""
    _, raw_key = valid_api_key
    payload = {
        "model": "non-existent-model-xyz",
        "messages": [{"role": "user", "content": "test"}],
    }
    response = await client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert response.status_code == 400

    data = response.json()
    assert "error" in data
    assert data["error"]["type"] == "invalid_request_error"
    assert "non-existent-model-xyz" in data["error"]["message"]


@pytest.mark.asyncio
async def test_chat_completions_validation_limits(
    client: AsyncClient, valid_api_key: tuple[ApiKey, str]
):
    """Violating request limits (empty messages, exceeding max ceiling) returns 422 or 400."""
    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}

    # Empty messages list (violates Pydantic min_length=1)
    res_empty = await client.post(
        "/v1/chat/completions",
        json={"model": "mock-primary", "messages": []},
        headers=headers,
    )
    assert res_empty.status_code == 422

    # Ceiling violation
    res_ceiling = await client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-primary",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 100000,
        },
        headers=headers,
    )
    assert res_ceiling.status_code == 400
    assert "ceiling" in res_ceiling.json()["error"]["message"]


@pytest.mark.asyncio
async def test_list_models_endpoint(client: AsyncClient):
    """GET /v1/models returns standard list containing concrete models and aliases."""
    response = await client.get("/v1/models")
    assert response.status_code == 200

    data = response.json()
    assert data["object"] == "list"
    model_ids = [m["id"] for m in data["data"]]

    # Ensure models and aliases are included
    assert "gemini-1.5-flash" in model_ids
    assert "gpt-4o-mini" in model_ids
    assert "mock-primary" in model_ids
    assert "default" in model_ids
    assert "mock-chain" in model_ids
