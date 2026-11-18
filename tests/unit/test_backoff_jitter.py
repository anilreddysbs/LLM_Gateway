"""Unit tests for exponential backoff, AWS full jitter, and Retry-After calculation."""

from llm_gateway.core.resilience.backoff import compute_backoff_delay


def test_backoff_jitter_bounds():
    """Jittered backoff must always remain between 0 and min(max_seconds, base * 2^attempt)."""
    base = 0.5
    max_sec = 8.0

    for attempt in range(6):
        ceiling = min(max_sec, base * (2**attempt))
        for _ in range(50):
            delay = compute_backoff_delay(attempt, base_seconds=base, max_seconds=max_sec)
            assert 0.0 <= delay <= ceiling


def test_backoff_retry_after_override():
    """If Retry-After is larger than computed jitter delay, Retry-After must be respected."""
    # Attempt 0: max jitter ceiling is 0.5s, but upstream asked for 3.0s
    delay = compute_backoff_delay(0, base_seconds=0.5, max_seconds=8.0, retry_after=3.0)
    assert delay >= 3.0

    # If retry_after is smaller than the calculated jitter, delay is >= retry_after
    delay_small = compute_backoff_delay(2, base_seconds=0.5, max_seconds=8.0, retry_after=0.1)
    assert delay_small >= 0.1
