"""Unit tests for configuration loading and models.yaml parsing."""

from decimal import Decimal

import pytest

from llm_gateway.config.models_config import load_models_config
from llm_gateway.config.settings import get_settings


def test_settings_load():
    """Verify settings defaults and environment mapping."""
    settings = get_settings()
    assert settings.PORT == 8000
    assert settings.FAIL_OPEN_ON_REDIS_ERROR is True
    assert settings.CACHE_TTL_SECONDS == 3600
    assert settings.MAX_RETRIES == 2


def test_models_config_loading():
    """Verify loading models.yaml parses models, Decimal prices, and aliases."""
    config = load_models_config("config/models.yaml")
    assert "gemini-1.5-flash" in config.models
    assert "gpt-4o-mini" in config.models
    assert "mock-primary" in config.models

    # Verify Decimal pricing
    flash = config.models["gemini-1.5-flash"]
    assert isinstance(flash.input_cost_per_million, Decimal)
    assert flash.input_cost_per_million == Decimal("0.075")
    assert flash.output_cost_per_million == Decimal("0.30")

    # Verify aliases
    assert "default" in config.aliases
    assert len(config.aliases["default"]) >= 2
    assert config.aliases["default"][0].provider == "gemini"


def test_routing_target_resolution():
    """Verify alias and direct model name target resolution."""
    config = load_models_config("config/models.yaml")

    # Alias resolution
    targets = config.resolve_targets("default")
    assert len(targets) >= 2
    assert targets[0].provider == "gemini"

    # Direct model name resolution
    targets_direct = config.resolve_targets("gpt-4o-mini")
    assert len(targets_direct) == 1
    assert targets_direct[0].provider == "openai"
    assert targets_direct[0].model == "gpt-4o-mini"

    # Unknown model
    with pytest.raises(ValueError, match="Unknown model or alias"):
        config.resolve_targets("non-existent-model")


def test_cost_calculation():
    """Verify precise Decimal cost calculation."""
    config = load_models_config("config/models.yaml")

    # 1,000,000 input tokens and 1,000,000 output tokens on gemini-1.5-flash:
    # 0.075 + 0.30 = 0.375
    cost = config.calculate_cost("gemini-1.5-flash", 1_000_000, 1_000_000)
    assert cost == Decimal("0.375000")

    # 1,000 input tokens and 500 output tokens:
    # (1000/1000000)*0.075 + (500/1000000)*0.30 = 0.000075 + 0.00015 = 0.000225
    cost_small = config.calculate_cost("gemini-1.5-flash", 1_000, 500)
    assert cost_small == Decimal("0.000225")
