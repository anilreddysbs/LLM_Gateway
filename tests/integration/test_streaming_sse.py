"""Integration tests for Server-Sent Events (SSE) streaming and early-fallback guarantees."""

import json

import pytest
from httpx import AsyncClient

from llm_gateway.db.models import ApiKey
from llm_gateway.providers.base import ProviderServerError
from llm_gateway.providers.factory import get_provider_factory
from llm_gateway.providers.mock import MockProvider


@pytest.mark.asyncio
async def test_streaming_sse_happy_path(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
):
    """Streaming request returns text/event-stream, delta chunks, and terminates with [DONE]."""
    factory = get_provider_factory()
    mock_prov = MockProvider(fixed_text="Hello world streaming test")
    factory.register_provider("mock", mock_prov)

    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {
        "model": "mock-primary",
        "messages": [{"role": "user", "content": "stream this"}],
        "stream": True,
    }

    response = await client.post("/v1/chat/completions", json=payload, headers=headers)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert response.headers["X-Gateway-Provider"] == "mock"

    lines = [line.strip() for line in response.text.split("\n") if line.strip()]

    # Must end with data: [DONE]
    assert lines[-1] == "data: [DONE]"

    # Verify first chunk sets role="assistant"
    first_data_line = lines[0]
    assert first_data_line.startswith("data: ")
    first_json = json.loads(first_data_line[6:])
    assert first_json["object"] == "chat.completion.chunk"
    assert first_json["choices"][0]["delta"]["role"] == "assistant"

    # Concatenate all content tokens
    collected_tokens = []
    for line in lines[:-1]:
        if line.startswith("data: "):
            chunk_data = json.loads(line[6:])
            content = chunk_data["choices"][0]["delta"].get("content", "")
            if content:
                collected_tokens.append(content)

    full_text = "".join(collected_tokens)
    assert "Hello world streaming test" in full_text


@pytest.mark.asyncio
async def test_streaming_fallback_before_first_chunk(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
):
    """When primary provider fails BEFORE the first chunk is emitted, fallback kicks in seamlessly."""
    factory = get_provider_factory()

    class FailingPrimaryMockProvider(MockProvider):
        async def stream(self, request):
            if request.model == "mock-primary":
                # Fails before yielding ANY chunk
                raise ProviderServerError("Primary stream crashed on init", status_code=500)
            async for chunk in super().stream(request):
                yield chunk

    custom_prov = FailingPrimaryMockProvider(fixed_text="Secondary stream arrived")
    factory.register_provider("mock", custom_prov)

    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {
        "model": "mock-chain",  # mock-primary -> mock-secondary
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    try:
        response = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        assert "Secondary " in response.text
        assert "stream " in response.text
        assert "arrived" in response.text
        assert "data: [DONE]" in response.text
    finally:
        factory.register_provider("mock", MockProvider())


@pytest.mark.asyncio
async def test_streaming_error_after_first_chunk_emits_sse_error(
    client: AsyncClient,
    valid_api_key: tuple[ApiKey, str],
):
    """When error occurs AFTER first chunk has been sent, fallback is forbidden; SSE error event is emitted."""
    factory = get_provider_factory()

    # Configure provider to fail midway through streaming (after 1st chunk)
    mock_prov = MockProvider(
        fixed_text="Word1 Word2 Word3",
        error_after_first_chunk=True,
    )
    factory.register_provider("mock", mock_prov)

    _, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}
    payload = {
        "model": "mock-chain",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    try:
        response = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert response.status_code == 200

        text = response.text
        # Ensure error event is delivered within the SSE stream
        assert "stream_interrupted" in text
        assert "Streaming interrupted mid-flight" in text
        assert "data: [DONE]" in text
    finally:
        factory.register_provider("mock", MockProvider())
