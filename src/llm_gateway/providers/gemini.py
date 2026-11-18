"""Google Gemini (Generative Language API) provider implementation."""

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


class GeminiProvider(BaseProvider):
    """Integrates with Google Generative Language API (v1beta)."""

    provider_name: str = "gemini"
    BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str | None = None, http_client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self._client = http_client

    def _get_client(self, timeout: float) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=timeout)

    def _build_gemini_payload(self, request: ProviderRequest) -> dict[str, Any]:
        """Convert standard OpenAI-style messages into Gemini API format."""
        contents: list[dict[str, Any]] = []
        system_instruction_text: str | None = None

        for msg in request.messages:
            if msg.role == "system":
                # Gemini handles system instructions in a dedicated field
                if system_instruction_text:
                    system_instruction_text += f"\n{msg.content}"
                else:
                    system_instruction_text = msg.content
            else:
                gemini_role = "user" if msg.role == "user" else "model"
                contents.append(
                    {
                        "role": gemini_role,
                        "parts": [{"text": msg.content}],
                    }
                )

        payload: dict[str, Any] = {"contents": contents}

        if system_instruction_text:
            payload["system_instruction"] = {"parts": [{"text": system_instruction_text}]}

        generation_config: dict[str, Any] = {}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens

        if generation_config:
            payload["generationConfig"] = generation_config

        return payload

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
            raise ProviderAuthError(f"Gemini authentication failed: {message}", status_code=status)
        if status == 429:
            raise ProviderRateLimitError(
                f"Gemini rate limit exceeded: {message}", retry_after=retry_after
            )
        if 400 <= status < 500:
            raise ProviderClientError(f"Gemini rejected request: {message}", status_code=status)
        if status >= 500:
            raise ProviderServerError(f"Gemini server error: {message}", status_code=status)
        raise ProviderError(f"Gemini error: {message}", status_code=status)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Call Gemini generateContent endpoint and parse response."""
        if not self.api_key:
            raise ProviderAuthError("GEMINI_API_KEY is not configured", status_code=401)

        url = f"{self.BASE_URL}/models/{request.model}:generateContent?key={self.api_key}"
        payload = self._build_gemini_payload(request)

        client = self._get_client(timeout=request.timeout_seconds)
        try:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                self._handle_error(response)

            data = response.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise ProviderServerError("Gemini returned empty candidates list", status_code=502)

            first_candidate = candidates[0]
            parts = first_candidate.get("content", {}).get("parts", [])
            text_result = parts[0].get("text", "") if parts else ""
            finish_reason = first_candidate.get("finishReason", "stop").lower()

            usage = data.get("usageMetadata", {})
            input_tokens = usage.get("promptTokenCount", 0)
            output_tokens = usage.get("candidatesTokenCount", 0)

            return ProviderResponse(
                text=text_result,
                model=request.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                finish_reason=finish_reason,
                raw_provider_id=self.provider_name,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"Gemini call timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ProviderConnectionError(f"Failed to connect to Gemini: {exc}") from exc

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderChunk]:
        """Stream Gemini responses using SSE format."""
        if not self.api_key:
            raise ProviderAuthError("GEMINI_API_KEY is not configured", status_code=401)

        url = f"{self.BASE_URL}/models/{request.model}:streamGenerateContent?alt=sse&key={self.api_key}"
        payload = self._build_gemini_payload(request)

        client = self._get_client(timeout=request.timeout_seconds)
        try:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    await response.aread()
                    self._handle_error(response)

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    json_str = line[6:].strip()
                    if not json_str:
                        continue

                    try:
                        data = json.loads(json_str)
                    except json.JSONDecodeError:
                        continue

                    candidates = data.get("candidates", [])
                    if not candidates:
                        continue

                    candidate = candidates[0]
                    parts = candidate.get("content", {}).get("parts", [])
                    text = parts[0].get("text", "") if parts else ""
                    raw_finish = candidate.get("finishReason")
                    finish_reason = raw_finish.lower() if raw_finish else None

                    usage = data.get("usageMetadata", {})
                    in_tok = usage.get("promptTokenCount")
                    out_tok = usage.get("candidatesTokenCount")

                    yield ProviderChunk(
                        text=text,
                        finish_reason=finish_reason,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        raw_provider_id=self.provider_name,
                    )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"Gemini streaming timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ProviderConnectionError(f"Failed to connect to Gemini stream: {exc}") from exc
