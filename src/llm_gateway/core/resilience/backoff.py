"""Exponential backoff calculation with full jitter and Retry-After support."""

import random


def compute_backoff_delay(
    attempt: int,
    base_seconds: float = 0.5,
    max_seconds: float = 8.0,
    retry_after: float | None = None,
) -> float:
    """Compute backoff delay using AWS Full Jitter algorithm.

    Formula:
        sleep = random.uniform(0, min(max_seconds, base_seconds * (2 ** attempt)))
    If upstream provided a Retry-After header, sleep at least that duration.
    """
    ceiling = min(max_seconds, base_seconds * (2**attempt))
    jittered_sleep = random.uniform(0, ceiling)

    if retry_after is not None and retry_after > 0:
        return max(jittered_sleep, retry_after)

    return jittered_sleep
