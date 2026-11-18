"""Pydantic schemas for administrative key management and usage inspection."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ApiKeyCreateRequest(BaseModel):
    """Payload for creating a new client API key."""

    name: str = Field(..., min_length=1, max_length=100, description="Friendly label for the API key.")
    requests_per_minute: int = Field(default=60, ge=1, le=10000, description="Rate limit ceiling in RPM.")
    monthly_budget_usd: Decimal = Field(
        default=Decimal("100.0000"),
        ge=Decimal("0.0"),
        description="Monthly spending limit in USD.",
    )
    isolate_cache: bool = Field(
        default=False,
        description="If True, caching for this key is completely isolated from other tenants.",
    )


class ApiKeyCreateResponse(BaseModel):
    """Returned exactly once upon key creation, revealing the plaintext secret."""

    id: str
    name: str
    raw_key: str = Field(..., description="Plaintext API key. Store this securely; it will NOT be shown again.")
    key_prefix: str
    requests_per_minute: int
    monthly_budget_usd: Decimal
    isolate_cache: bool
    created_at: datetime


class ApiKeyResponse(BaseModel):
    """Sanitized view of a client API key (never exposes the raw secret or full hash)."""

    id: str
    name: str
    key_prefix: str
    requests_per_minute: int
    monthly_budget_usd: Decimal
    is_revoked: bool
    isolate_cache: bool
    created_at: datetime
    updated_at: datetime


class ApiKeyListResponse(BaseModel):
    """List container for admin API keys query."""

    keys: list[ApiKeyResponse]
    total: int


class UsageLogRow(BaseModel):
    """A single request usage and telemetry audit record."""

    id: str
    request_id: str
    api_key_id: str | None
    created_at: datetime
    alias: str | None
    requested_model: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: float
    cache_hit: bool
    fallback_used: bool
    attempts: int
    status_code: int
    error_type: str | None
    streaming: bool
    estimated_tokens: bool


class UsageQueryResponse(BaseModel):
    """Paginated list of usage log audit records."""

    logs: list[UsageLogRow]
    total: int


class AdminStatsResponse(BaseModel):
    """Aggregated operational statistics and cost breakdowns."""

    total_requests: int
    cache_hit_rate: float
    error_rate: float
    fallback_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    total_tokens: int
    total_cost_usd: Decimal
    cost_by_model: dict[str, Decimal]
    cost_by_provider: dict[str, Decimal]
    requests_per_day: dict[str, int]
