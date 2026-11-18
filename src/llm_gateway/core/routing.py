"""Routing engine for alias resolution and provider target selection."""

from llm_gateway.config.models_config import ModelsConfig, RoutingTarget, load_models_config
from llm_gateway.config.settings import get_settings


class RequestRouter:
    """Resolves client requested model names or aliases into ordered provider targets."""

    def __init__(self, config: ModelsConfig | None = None):
        if config is None:
            settings = get_settings()
            config = load_models_config(settings.MODELS_CONFIG_PATH)
        self.config = config

    def resolve(self, model_or_alias: str) -> list[RoutingTarget]:
        """Resolve a requested model or alias to an ordered list of targets.

        Raises ValueError if the model or alias is not found.
        """
        return self.config.resolve_targets(model_or_alias)


_default_router: RequestRouter | None = None


def get_router() -> RequestRouter:
    """Retrieve or create singleton RequestRouter."""
    global _default_router
    if _default_router is None:
        _default_router = RequestRouter()
    return _default_router
