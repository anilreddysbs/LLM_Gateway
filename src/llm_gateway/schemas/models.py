"""Schemas for model catalog and routing alias discovery."""

import time
from typing import Literal

from pydantic import BaseModel, Field


class ModelCard(BaseModel):
    """An individual model entry conforming to OpenAI's model listing schema."""

    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "llm-gateway"
    provider: str | None = None
    input_cost_per_million: str | None = None
    output_cost_per_million: str | None = None


class ModelListResponse(BaseModel):
    """OpenAI standard response format for GET /v1/models."""

    object: Literal["list"] = "list"
    data: list[ModelCard]
