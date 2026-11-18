"""MockProvider for deterministic testing of retries, timeouts, circuit breaking, and fallbacks."""

import asyncio
from collections.abc import AsyncIterator

from llm_gateway.providers.base import (
    BaseProvider,
    ProviderChunk,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderResponse,
    ProviderServerError,
    ProviderTimeoutError,
)


class MockProvider(BaseProvider):
    """Configurable mock provider that simulates real-world LLM behavior and fault modes."""

    provider_name: str = "mock"

    def __init__(
        self,
        fixed_text: str = "Hello from MockProvider!",
        latency_seconds: float = 0.0,
        fail_n_times: int = 0,
        always_fail_status: int | None = None,
        simulate_timeout: bool = False,
        error_after_first_chunk: bool = False,
        input_tokens: int = 15,
        output_tokens: int = 25,
    ):
        self.fixed_text = fixed_text
        self.latency_seconds = latency_seconds
        self.fail_n_times = fail_n_times
        self._current_failures = 0
        self.always_fail_status = always_fail_status
        self.simulate_timeout = simulate_timeout
        self.error_after_first_chunk = error_after_first_chunk
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.call_count = 0

    def reset(self) -> None:
        """Reset internal counters for reproducible tests."""
        self._current_failures = 0
        self.call_count = 0

    def _check_fault_injection(self) -> None:
        """Evaluate whether to trigger a configured failure mode."""
        self.call_count += 1

        if self.simulate_timeout:
            raise ProviderTimeoutError("Mock provider simulated timeout")

        if self.always_fail_status is not None:
            if self.always_fail_status == 429:
                raise ProviderRateLimitError("Mock provider rate limited (429)", retry_after=1.0)
            if self.always_fail_status >= 500:
                raise ProviderServerError(
                    f"Mock provider internal error ({self.always_fail_status})",
                    status_code=self.always_fail_status,
                )
            raise ProviderError(
                f"Mock provider failed with status {self.always_fail_status}",
                status_code=self.always_fail_status,
            )

        if self._current_failures < self.fail_n_times:
            self._current_failures += 1
            raise ProviderServerError(
                f"Mock provider transient error ({self._current_failures}/{self.fail_n_times})",
                status_code=500,
            )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return non-streaming mock completion or trigger simulated fault."""
        if self.latency_seconds > 0:
            await asyncio.sleep(self.latency_seconds)

        self._check_fault_injection()

        text = (
            self.fixed_text.format(model=request.model)
            if "{model}" in self.fixed_text
            else self.fixed_text
        )
        return ProviderResponse(
            text=text,
            model=request.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            finish_reason="stop",
            raw_provider_id=self.provider_name,
        )

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderChunk]:
        """Yield streaming mock chunks or simulate faults before or after the first chunk."""
        if self.latency_seconds > 0:
            await asyncio.sleep(self.latency_seconds)

        # Fault before any chunk is yielded
        self._check_fault_injection()

        text = (
            self.fixed_text.format(model=request.model)
            if "{model}" in self.fixed_text
            else self.fixed_text
        )
        words = text.split(" ")

        for idx, word in enumerate(words):
            # Check fault injection midway through streaming (after 1st chunk)
            if self.error_after_first_chunk and idx == 1:
                raise ProviderServerError(
                    "Mock streaming failed after first chunk yielded", status_code=500
                )

            chunk_text = word if idx == len(words) - 1 else f"{word} "
            is_last = idx == len(words) - 1

            yield ProviderChunk(
                text=chunk_text,
                finish_reason="stop" if is_last else None,
                input_tokens=self.input_tokens if is_last else None,
                output_tokens=self.output_tokens if is_last else None,
                raw_provider_id=self.provider_name,
            )
            # Small yield pause for streaming effect
            await asyncio.sleep(0.005)
