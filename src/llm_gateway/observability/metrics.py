"""Prometheus metrics registry and telemetry collectors for the LLM Gateway."""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# 1. Request volume counter
REQUESTS_TOTAL = Counter(
    "llm_gateway_requests_total",
    "Total completion requests processed by the gateway",
    ["provider", "model", "status_code", "streaming"],
)

# 2. Request duration latency histogram
REQUEST_DURATION_SECONDS = Histogram(
    "llm_gateway_request_duration_seconds",
    "End-to-end latency of completion requests in seconds",
    ["provider", "model"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# 3. Token throughput counter
TOKENS_TOTAL = Counter(
    "llm_gateway_tokens_total",
    "Total tokens processed (prompt or completion)",
    ["provider", "model", "type"],
)

# 4. Total USD spend counter
COST_USD_TOTAL = Counter(
    "llm_gateway_cost_usd_total",
    "Total USD spend accrued across models and providers",
    ["provider", "model"],
)

# 5. Response cache effectiveness
CACHE_REQUESTS_TOTAL = Counter(
    "llm_gateway_cache_requests_total",
    "Total cache inquiries partitioned by cache status",
    ["status"],  # "hit" | "miss"
)

# 6. Retry attempt counter
RETRIES_TOTAL = Counter(
    "llm_gateway_retries_total",
    "Total retry attempts executed across providers and models",
    ["provider", "model"],
)

# 7. Fallback routing occurrences
FALLBACKS_TOTAL = Counter(
    "llm_gateway_fallbacks_total",
    "Total fallback hops from one provider to another",
    ["from_provider", "to_provider"],
)

# 8. Circuit breaker operational state
# 0 = CLOSED (Normal), 1 = HALF_OPEN (Probing), 2 = OPEN (Failing/Tripped)
CIRCUIT_BREAKER_STATE = Gauge(
    "llm_gateway_circuit_breaker_state",
    "Current state of provider circuit breaker (0=CLOSED, 1=HALF_OPEN, 2=OPEN)",
    ["provider", "model"],
)

# 9. Rate limiting rejections
RATE_LIMIT_EXCEEDED_TOTAL = Counter(
    "llm_gateway_rate_limit_exceeded_total",
    "Total 429 rate limit rejections per client API key",
    ["api_key_id"],
)

# 10. Log queue saturation drops
DROPPED_LOGS_TOTAL = Counter(
    "llm_gateway_dropped_logs_total",
    "Total usage log records dropped due to background queue saturation",
)


def get_metrics_content() -> tuple[bytes, str]:
    """Render latest Prometheus metrics payload and content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
