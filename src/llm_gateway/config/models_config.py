"""Models and pricing configuration loader from YAML with Decimal precision."""

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ModelDefinition(BaseModel):
    """Specification and pricing metadata for a specific upstream model."""

    provider: str
    model_id: str
    input_cost_per_million: Decimal = Field(
        ...,
        description="USD cost per 1M input tokens as Decimal for precise calculation",
    )
    output_cost_per_million: Decimal = Field(
        ...,
        description="USD cost per 1M output tokens as Decimal for precise calculation",
    )
    max_tokens: int = 4096
    supports_streaming: bool = True


class RoutingTarget(BaseModel):
    """A single provider and model target within a fallback chain."""

    provider: str
    model: str


class ModelsConfig(BaseModel):
    """Container for all configured models and routing aliases."""

    models: dict[str, ModelDefinition] = Field(default_factory=dict)
    aliases: dict[str, list[RoutingTarget]] = Field(default_factory=dict)

    def resolve_targets(self, model_or_alias: str) -> list[RoutingTarget]:
        """Resolve a requested model or alias into an ordered list of fallback targets.

        If model_or_alias matches an alias, returns the configured chain.
        If it matches a known model, returns a single-target list.
        Otherwise raises ValueError.
        """
        if model_or_alias in self.aliases:
            return self.aliases[model_or_alias]
        if model_or_alias in self.models:
            model_info = self.models[model_or_alias]
            return [RoutingTarget(provider=model_info.provider, model=model_info.model_id)]
        raise ValueError(f"Unknown model or alias: '{model_or_alias}'")

    def get_model(self, model_name: str) -> ModelDefinition | None:
        """Lookup model definition by name or model_id."""
        if model_name in self.models:
            return self.models[model_name]
        for defn in self.models.values():
            if defn.model_id == model_name:
                return defn
        return None

    def calculate_cost(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        """Calculate total USD cost using Decimal arithmetic."""
        defn = self.get_model(model_name)
        if not defn:
            return Decimal("0.0")

        # Decimal precision: (tokens / 1_000_000) * price_per_million
        million = Decimal("1000000")
        in_cost = (Decimal(input_tokens) / million) * defn.input_cost_per_million
        out_cost = (Decimal(output_tokens) / million) * defn.output_cost_per_million
        return (in_cost + out_cost).quantize(Decimal("0.000001"))


def load_models_config(config_path: str | Path = "config/models.yaml") -> ModelsConfig:
    """Load and parse models configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Models configuration file not found at: {path}")

    with open(path, encoding="utf-8") as f:
        raw_data = yaml.safe_load(f) or {}

    raw_models = raw_data.get("models", {})
    raw_aliases = raw_data.get("aliases", {})

    models: dict[str, ModelDefinition] = {}
    for key, data in raw_models.items():
        models[key] = ModelDefinition(
            provider=data["provider"],
            model_id=data.get("model_id", key),
            input_cost_per_million=Decimal(str(data["input_cost_per_million"])),
            output_cost_per_million=Decimal(str(data["output_cost_per_million"])),
            max_tokens=data.get("max_tokens", 4096),
            supports_streaming=data.get("supports_streaming", True),
        )

    aliases: dict[str, list[RoutingTarget]] = {}
    for alias_name, targets in raw_aliases.items():
        aliases[alias_name] = [
            RoutingTarget(provider=t["provider"], model=t["model"]) for t in targets
        ]

    return ModelsConfig(models=models, aliases=aliases)
