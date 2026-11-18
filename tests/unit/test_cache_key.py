"""Unit tests for cache key normalization, tenant isolation, and eligibility checks."""

from llm_gateway.config.settings import Settings
from llm_gateway.core.cache import (
    is_cache_eligible,
    normalize_request_for_cache,
    parse_cache_control,
)
from llm_gateway.db.models import ApiKey
from llm_gateway.schemas.chat import ChatCompletionRequest, ChatMessage


def test_cache_key_normalization_deterministic():
    """Identical requests with different message whitespace produce the same SHA-256 cache key."""
    req1 = ChatCompletionRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="  Hello World  ")],
        temperature=0.2,
    )
    req2 = ChatCompletionRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="Hello World")],
        temperature=0.2,
    )

    key1 = normalize_request_for_cache(req1)
    key2 = normalize_request_for_cache(req2)
    assert key1 == key2
    assert key1.startswith("llm_gateway:cache:v1:")


def test_cache_key_shared_vs_isolated():
    """Shared cache yields same key across keys; isolated cache yields unique keys per tenant."""
    req = ChatCompletionRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="Same prompt")],
        temperature=0.1,
    )
    key_a = ApiKey(id="tenant-a", name="Tenant A", key_hash="ha", key_prefix="pa")
    key_b = ApiKey(id="tenant-b", name="Tenant B", key_hash="hb", key_prefix="pb")

    # Shared cache (default)
    shared_a = normalize_request_for_cache(req, api_key=key_a, isolate_cache=False)
    shared_b = normalize_request_for_cache(req, api_key=key_b, isolate_cache=False)
    assert shared_a == shared_b

    # Isolated cache
    isolated_a = normalize_request_for_cache(req, api_key=key_a, isolate_cache=True)
    isolated_b = normalize_request_for_cache(req, api_key=key_b, isolate_cache=True)
    assert isolated_a != isolated_b
    assert isolated_a != shared_a


def test_parse_cache_control_headers():
    """Verify parsing of Cache-Control header directives."""
    assert parse_cache_control(None) == (False, False)
    assert parse_cache_control("") == (False, False)
    assert parse_cache_control("no-cache") == (True, False)
    assert parse_cache_control("no-store") == (False, True)
    assert parse_cache_control("no-cache, no-store") == (True, True)
    assert parse_cache_control("max-age=0, no-cache") == (True, False)


def test_is_cache_eligible():
    """Only non-streaming requests with temperature <= 0.3 are cache-eligible by default."""
    settings = Settings(CACHE_ENABLED=True, CACHE_MAX_TEMPERATURE=0.3)

    # Eligible
    req_ok = ChatCompletionRequest(
        model="mock-primary",
        messages=[ChatMessage(role="user", content="hi")],
        temperature=0.2,
        stream=False,
    )
    assert is_cache_eligible(req_ok, settings) is True

    # High temperature (> 0.3) -> Ineligible
    req_high_temp = ChatCompletionRequest(
        model="mock-primary",
        messages=[ChatMessage(role="user", content="hi")],
        temperature=0.7,
        stream=False,
    )
    assert is_cache_eligible(req_high_temp, settings) is False

    # Streaming -> Ineligible
    req_stream = ChatCompletionRequest(
        model="mock-primary",
        messages=[ChatMessage(role="user", content="hi")],
        temperature=0.1,
        stream=True,
    )
    assert is_cache_eligible(req_stream, settings) is False
