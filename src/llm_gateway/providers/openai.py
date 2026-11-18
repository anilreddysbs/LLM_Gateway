"""OpenAI-compatible provider implementation (works with OpenAI, Groq, OpenRouter, vLLM)."""

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from llm_gateway.providers.base import (
    BaseProvider,
    ProviderAuthError,
    ProviderChunk,
    ProviderClientError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderResponse,
    ProviderServerError,
    ProviderTimeoutError,
)


class OpenAICompatibleProvider(BaseProvider):
    """Communicates with any standard OpenAI-compatible completions endpoint."""

    provider_name: str = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        http_client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = http_client

    def _get_client(self, timeout: float) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=timeout)

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _handle_error(self, response: httpx.Response) -> None:
        """Map HTTP error status codes to unified ProviderError hierarchy."""
        status = response.status_code
        retry_after = None
        if "retry-after" in response.headers:
            with contextlib.suppress(ValueError, TypeError):
                retry_after = float(response.headers["retry-after"])

        try:
            error_json = response.json()
            message = error_json.get("error", {}).get("message", response.text)
        except Exception:
            message = response.text or f"HTTP {status}"

        if status in (401, 403):
            raise ProviderAuthError(f"OpenAI authentication failed: {message}", status_code=status)
        if status == 429:
            raise ProviderRateLimitError(
                f"OpenAI rate limit exceeded: {message}", retry_after=retry_after
            )
        if 400 <= status < 500:
            raise ProviderClientError(f"OpenAI rejected request: {message}", status_code=status)
        if status >= 500:
            raise ProviderServerError(f"OpenAI server error: {message}", status_code=status)
        raise ProviderError(f"OpenAI error: {message}", status_code=status)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Execute non-streaming completion against /chat/completions."""
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [msg.model_dump(exclude_none=True) for msg in request.messages],
            "stream": False,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        client = self._get_client(timeout=request.timeout_seconds)
        try:
            response = await client.post(url, headers=self._get_headers(), json=payload)
            if response.status_code != 200:
                self._handle_error(response)

            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                raise ProviderServerError("OpenAI returned empty choices", status_code=502)

            first_choice = choices[0]
            text = first_choice.get("message", {}).get("content", "")
            finish_reason = first_choice.get("finish_reason", "stop")

            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            return ProviderResponse(
                text=text,
                model=data.get("model", request.model),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                finish_reason=finish_reason,
                raw_provider_id=self.provider_name,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"OpenAI call timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ProviderConnectionError(f"Failed to connect to OpenAI: {exc}") from exc

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderChunk]:
        """Execute streaming completion yielding normalized chunks."""
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [msg.model_dump(exclude_none=True) for msg in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        client = self._get_client(timeout=request.timeout_seconds)
        try:
            async with client.stream(
                "POST", url, headers=self._get_headers(), json=payload
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    self._handle_error(response)

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk_dict = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk_dict.get("choices", [])
                    delta_text = ""
                    finish_reason = None
                    if choices:
                        choice = choices[0]
                        delta_text = choice.get("delta", {}).get("content", "")
                        finish_reason = choice.get("finish_reason")

                    usage = chunk_dict.get("usage")
                    in_tok = usage.get("prompt_tokens") if usage else None
                    out_tok = usage.get("completion_tokens") if usage else None

                    yield ProviderChunk(
                        text=delta_text,
                        finish_reason=finish_reason,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        raw_provider_id=self.provider_name,
                    )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"OpenAI streaming timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ProviderConnectionError(f"Failed to connect to OpenAI stream: {exc}") from exc
