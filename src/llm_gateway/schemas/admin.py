"""Pydantic schemas for administrative key management and usage inspection."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ApiKeyCreateRequest(BaseModel):
    """Payload for creating a new client API key."""

    name: str = Field(
        ..., min_length=1, max_length=100, description="Friendly label for the API key."
    )
    requests_per_minute: int = Field(
        default=60, ge=1, le=10000, description="Rate limit ceiling in RPM."
    )
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
    raw_key: str = Field(
        ..., description="Plaintext API key. Store this securely; it will NOT be shown again."
    )
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
