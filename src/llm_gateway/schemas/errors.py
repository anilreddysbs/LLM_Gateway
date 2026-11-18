"""OpenAI-compatible error response schemas and HTTP exception handlers."""

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ErrorDetails(BaseModel):
    """Detailed error object conforming to the OpenAI API specification."""

    message: str = Field(..., description="A human-readable explanation of the error.")
    type: str = Field(
        ..., description="Error classification (e.g. invalid_request_error, rate_limit_error)."
    )
    code: str | None = Field(None, description="Short machine-readable error code if applicable.")
    param: str | None = Field(None, description="Parameter related to the error if applicable.")


class OpenAIErrorResponse(BaseModel):
    """Top-level OpenAI-compatible error response wrapper."""

    error: ErrorDetails


class GatewayException(HTTPException):
    """Custom HTTP exception emitting standard OpenAI error JSON payload."""

    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str = "api_error",
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        detail: dict[str, Any] = {
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
                "param": param,
            }
        }
        super().__init__(status_code=status_code, detail=detail, headers=headers)
