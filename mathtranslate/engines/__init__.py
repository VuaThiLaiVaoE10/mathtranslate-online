from typing import Dict, Type

from .base import BaseTranslator, build_academic_system_prompt
from .deepl_engine import DeepLTranslator, normalize_deepl_language
from .gemini_engine import GeminiTranslator
from .google_engine import GoogleTranslator
from .openai_compat import OllamaTranslator, OpenAICompatTranslator, OpenAITranslator
from .tencent_engine import TencentTranslator


AVAILABLE_ENGINES = ("google", "tencent", "gemini", "openai", "ollama", "deepl")

ENGINE_REGISTRY: Dict[str, Type[BaseTranslator]] = {
    "google": GoogleTranslator,
    "tencent": TencentTranslator,
    "gemini": GeminiTranslator,
    "openai": OpenAITranslator,
    "ollama": OllamaTranslator,
    "deepl": DeepLTranslator,
}


def create_engine(engine_name: str, **kwargs) -> BaseTranslator:
    """Instantiate a translation engine by name from AVAILABLE_ENGINES."""
    if not engine_name or not isinstance(engine_name, str):
        raise ValueError(
            f"Invalid engine name '{engine_name}'. "
            f"Available engines: {', '.join(AVAILABLE_ENGINES)}"
        )
    normalized = engine_name.strip().lower()
    if normalized not in ENGINE_REGISTRY:
        raise ValueError(
            f"Unknown translation engine '{engine_name}'. "
            f"Available engines: {', '.join(AVAILABLE_ENGINES)}"
        )
    engine_cls = ENGINE_REGISTRY[normalized]
    return engine_cls(**kwargs)


__all__ = [
    "AVAILABLE_ENGINES",
    "ENGINE_REGISTRY",
    "BaseTranslator",
    "GoogleTranslator",
    "TencentTranslator",
    "OpenAICompatTranslator",
    "OpenAITranslator",
    "OllamaTranslator",
    "GeminiTranslator",
    "DeepLTranslator",
    "build_academic_system_prompt",
    "normalize_deepl_language",
    "create_engine",
]
