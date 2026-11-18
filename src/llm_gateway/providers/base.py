"""Abstract provider base class and unified normalized data structures."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from llm_gateway.schemas.chat import ChatMessage


class ProviderRequest(BaseModel):
    """Normalized request passed down to concrete upstream provider implementations."""

    model: str = Field(..., description="Target upstream model name (e.g. gemini-1.5-flash).")
    messages: list[ChatMessage] = Field(..., description="Conversation messages.")
    temperature: float | None = Field(default=1.0)
    max_tokens: int | None = Field(default=None)
    timeout_seconds: float = Field(default=15.0)


class ProviderResponse(BaseModel):
    """Normalized completion result returned by all upstream providers."""

    text: str = Field(..., description="Full text completion produced by the model.")
    model: str = Field(..., description="The actual model name that generated the text.")
    input_tokens: int = Field(default=0, description="Count of prompt input tokens.")
    output_tokens: int = Field(default=0, description="Count of completion output tokens.")
    finish_reason: str = Field(default="stop", description="Stop condition (e.g. stop, length).")
    raw_provider_id: str = Field(
        ..., description="Identifier of the provider (e.g. gemini, openai, mock)."
    )


class ProviderChunk(BaseModel):
    """Normalized streaming delta emitted by complete stream generators."""

    text: str = Field(default="", description="Delta text chunk.")
    finish_reason: str | None = Field(
        default=None, description="Stop condition if this is the final chunk."
    )
    input_tokens: int | None = Field(
        default=None, description="Input token count if reported by provider."
    )
    output_tokens: int | None = Field(
        default=None, description="Output token count if reported by provider."
    )
    raw_provider_id: str = Field(default="", description="Provider identifier.")


# Provider Exception Hierarchy
class ProviderError(Exception):
    """Base exception for all provider communication failures."""

    def __init__(self, message: str, status_code: int = 500, retry_after: float | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after


class ProviderTimeoutError(ProviderError):
    """Raised when an attempt to an upstream provider exceeds timeout limits."""

    def __init__(self, message: str = "Provider request timed out"):
        super().__init__(message, status_code=504)


class ProviderConnectionError(ProviderError):
    """Raised on network connection refusal or DNS resolution failure."""

    def __init__(self, message: str = "Failed to connect to provider"):
        super().__init__(message, status_code=502)


class ProviderRateLimitError(ProviderError):
    """Raised when upstream returns HTTP 429 Too Many Requests."""

    def __init__(
        self,
        message: str = "Upstream provider rate limit exceeded",
        retry_after: float | None = None,
    ):
        super().__init__(message, status_code=429, retry_after=retry_after)


class ProviderServerError(ProviderError):
    """Raised when upstream returns 5xx internal server errors."""

    def __init__(self, message: str = "Upstream provider 5xx error", status_code: int = 502):
        super().__init__(message, status_code=status_code)


class ProviderClientError(ProviderError):
    """Raised when upstream returns non-retryable 4xx client errors (400, 404, etc.)."""

    def __init__(self, message: str = "Upstream provider rejected request", status_code: int = 400):
        super().__init__(message, status_code=status_code)


class ProviderAuthError(ProviderClientError):
    """Raised when provider API key is missing or invalid (401/403)."""

    def __init__(
        self, message: str = "Upstream provider authentication failed", status_code: int = 401
    ):
        super().__init__(message, status_code=status_code)


class BaseProvider(ABC):
    """Abstract Base Class specifying the contract for all LLM providers."""

    provider_name: str

    @abstractmethod
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Execute a non-streaming chat completion."""
        pass

    @abstractmethod
    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderChunk]:
        """Execute a streaming chat completion yielding normalized chunks."""
        pass
