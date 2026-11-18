"""Unit tests for Provider implementations (Mock, Gemini, OpenAI-compatible)."""

import httpx
import pytest
import respx

from llm_gateway.providers.base import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderServerError,
    ProviderTimeoutError,
)
from llm_gateway.providers.gemini import GeminiProvider
from llm_gateway.providers.mock import MockProvider
from llm_gateway.providers.openai import OpenAICompatibleProvider
from llm_gateway.schemas.chat import ChatMessage


@pytest.mark.asyncio
async def test_mock_provider_happy_path():
    """MockProvider returns normalized ProviderResponse with requested model."""
    provider = MockProvider(fixed_text="Custom mock text")
    req = ProviderRequest(
        model="mock-primary",
        messages=[ChatMessage(role="user", content="Hello")],
    )
    resp = await provider.complete(req)
    assert resp.text == "Custom mock text"
    assert resp.model == "mock-primary"
    assert resp.input_tokens > 0
    assert resp.output_tokens > 0
    assert resp.finish_reason == "stop"
    assert resp.raw_provider_id == "mock"


@pytest.mark.asyncio
async def test_mock_provider_fault_injection():
    """MockProvider correctly raises configured errors."""
    # Timeout
    timeout_provider = MockProvider(simulate_timeout=True)
    with pytest.raises(ProviderTimeoutError):
        await timeout_provider.complete(
            ProviderRequest(model="mock-primary", messages=[ChatMessage(role="user", content="hi")])
        )

    # 429 Rate limit
    rate_limit_provider = MockProvider(always_fail_status=429)
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await rate_limit_provider.complete(
            ProviderRequest(model="mock-primary", messages=[ChatMessage(role="user", content="hi")])
        )
    assert exc_info.value.retry_after == 1.0

    # 500 Server error
    server_error_provider = MockProvider(always_fail_status=500)
    with pytest.raises(ProviderServerError):
        await server_error_provider.complete(
            ProviderRequest(model="mock-primary", messages=[ChatMessage(role="user", content="hi")])
        )

    # Fail N times then succeed
    fail_twice_provider = MockProvider(fail_n_times=2, fixed_text="Recovered")
    with pytest.raises(ProviderServerError):
        await fail_twice_provider.complete(
            ProviderRequest(model="mock-primary", messages=[ChatMessage(role="user", content="1")])
        )
    with pytest.raises(ProviderServerError):
        await fail_twice_provider.complete(
            ProviderRequest(model="mock-primary", messages=[ChatMessage(role="user", content="2")])
        )
    # 3rd attempt succeeds
    resp = await fail_twice_provider.complete(
        ProviderRequest(model="mock-primary", messages=[ChatMessage(role="user", content="3")])
    )
    assert resp.text == "Recovered"


@pytest.mark.asyncio
@respx.mock
async def test_gemini_provider_complete():
    """GeminiProvider maps payload, handles response, and calculates tokens."""
    api_key = "fake-gemini-key"
    model = "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    gemini_response = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Gemini response text"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 24,
        },
    }

    respx.post(url).mock(return_value=httpx.Response(200, json=gemini_response))

    provider = GeminiProvider(api_key=api_key)
    req = ProviderRequest(
        model=model,
        messages=[
            ChatMessage(role="system", content="Be helpful"),
            ChatMessage(role="user", content="Hello Gemini"),
        ],
    )
    resp = await provider.complete(req)
    assert resp.text == "Gemini response text"
    assert resp.input_tokens == 12
    assert resp.output_tokens == 24
    assert resp.finish_reason == "stop"
    assert resp.raw_provider_id == "gemini"


@pytest.mark.asyncio
@respx.mock
async def test_gemini_provider_auth_error():
    """GeminiProvider raises ProviderAuthError on 401/403."""
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=invalid-key"
    respx.post(url).mock(
        return_value=httpx.Response(403, json={"error": {"message": "API key not valid"}})
    )

    provider = GeminiProvider(api_key="invalid-key")
    with pytest.raises(ProviderAuthError):
        await provider.complete(
            ProviderRequest(
                model="gemini-1.5-flash", messages=[ChatMessage(role="user", content="hi")]
            )
        )


@pytest.mark.asyncio
@respx.mock
async def test_openai_compatible_provider_complete():
    """OpenAICompatibleProvider sends standard payload and unpacks choices."""
    url = "https://api.openai.com/v1/chat/completions"
    openai_response = {
        "id": "chatcmpl-test",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from OpenAI compatible"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }

    respx.post(url).mock(return_value=httpx.Response(200, json=openai_response))

    provider = OpenAICompatibleProvider(api_key="sk-test", base_url="https://api.openai.com/v1")
    req = ProviderRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="Hello")],
    )
    resp = await provider.complete(req)
    assert resp.text == "Hello from OpenAI compatible"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 20
    assert resp.raw_provider_id == "openai"
