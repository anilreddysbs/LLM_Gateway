"""Model discovery endpoints."""

from fastapi import APIRouter, Depends

from llm_gateway.config.models_config import ModelsConfig, load_models_config
from llm_gateway.config.settings import get_settings
from llm_gateway.schemas.models import ModelCard, ModelListResponse

router = APIRouter(prefix="/v1", tags=["Models"])


def get_models_config_dep() -> ModelsConfig:
    settings = get_settings()
    return load_models_config(settings.MODELS_CONFIG_PATH)


@router.get("/models", response_model=ModelListResponse)
async def list_models(config: ModelsConfig = Depends(get_models_config_dep)) -> ModelListResponse:
    """List available upstream models and routing aliases."""
    cards: list[ModelCard] = []

    # 1. Concrete models
    for key, model_defn in config.models.items():
        cards.append(
            ModelCard(
                id=key,
                provider=model_defn.provider,
                input_cost_per_million=str(model_defn.input_cost_per_million),
                output_cost_per_million=str(model_defn.output_cost_per_million),
            )
        )

    # 2. Aliases as virtual models
    for alias_name in config.aliases:
        cards.append(
            ModelCard(
                id=alias_name,
                provider="alias",
                owned_by="gateway-alias",
            )
        )

    return ModelListResponse(data=cards)
