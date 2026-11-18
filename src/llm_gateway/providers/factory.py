"""Provider registry and factory."""

from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.providers.base import BaseProvider
from llm_gateway.providers.gemini import GeminiProvider
from llm_gateway.providers.mock import MockProvider
from llm_gateway.providers.openai import OpenAICompatibleProvider


class ProviderFactory:
    """Manages active provider instances with support for test overrides."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._providers: dict[str, BaseProvider] = {}
        self._init_default_providers()

    def _init_default_providers(self) -> None:
        """Instantiate configured providers."""
        self._providers["gemini"] = GeminiProvider(api_key=self.settings.GEMINI_API_KEY)
        self._providers["openai"] = OpenAICompatibleProvider(
            api_key=self.settings.OPENAI_API_KEY,
            base_url=self.settings.OPENAI_BASE_URL,
        )
        self._providers["mock"] = MockProvider()

    def get_provider(self, provider_name: str) -> BaseProvider:
        """Retrieve provider instance by name. Raises ValueError if unknown."""
        name = provider_name.lower()
        if name not in self._providers:
            raise ValueError(
                f"Unknown provider: '{provider_name}'. Available: {list(self._providers.keys())}"
            )
        return self._providers[name]

    def register_provider(self, name: str, provider: BaseProvider) -> None:
        """Register or override a provider instance (useful for test harnesses)."""
        self._providers[name.lower()] = provider


_default_factory: ProviderFactory | None = None


def get_provider_factory() -> ProviderFactory:
    """Singleton accessor for the global provider factory."""
    global _default_factory
    if _default_factory is None:
        _default_factory = ProviderFactory()
    return _default_factory
