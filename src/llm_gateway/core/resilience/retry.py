"""Error classification and retry policies for LLM upstream requests."""

from llm_gateway.providers.base import (
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)


def is_retriable_error(exc: Exception) -> bool:
    """Classify whether an exception qualifies for exponential backoff and retry.

    Retriable:
        - Upstream timeouts (504, ProviderTimeoutError)
        - Connection and DNS errors (502, ProviderConnectionError)
        - Rate limits (429, ProviderRateLimitError)
        - Internal server errors (5xx, ProviderServerError)

    Non-retriable:
        - 4xx client errors (400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found)
    """
    if isinstance(exc, (ProviderTimeoutError, ProviderConnectionError, ProviderRateLimitError)):
        return True

    if isinstance(exc, ProviderServerError):
        return True

    if isinstance(exc, ProviderError):
        # 429 or 5xx are retriable; other 4xx are client errors and must not be retried
        return exc.status_code == 429 or exc.status_code >= 500

    return False


def get_retry_after(exc: Exception) -> float | None:
    """Extract Retry-After duration in seconds if present on exception."""
    if isinstance(exc, ProviderError) and exc.retry_after is not None:
        return exc.retry_after
    return None
