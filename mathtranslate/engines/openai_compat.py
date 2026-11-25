import os
from typing import Optional
import requests

from ..config import config
from .base import BaseTranslator, build_academic_system_prompt


class OpenAICompatTranslator(BaseTranslator):
    """OpenAI-compatible chat completions translator for OpenAI and local Ollama."""

    name = "openai"
    supports_multithread = True

    OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
    OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
    OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"
    OLLAMA_DEFAULT_MODEL = "qwen2.5:7b"

    def __init__(
        self,
        provider: str = "openai",
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        temperature: float = 0.1,
        **_kwargs,
    ):
        norm_provider = (provider or "openai").strip().lower()
        if norm_provider not in ("openai", "ollama"):
            raise ValueError(f"Unsupported OpenAI-compatible provider: {provider}")
        self.provider = norm_provider
        self.name = norm_provider
        self.timeout = timeout
        self.temperature = temperature

        if self.provider == "openai":
            resolved_key = (
                api_key
                if api_key is not None
                else (
                    os.environ.get("OPENAI_API_KEY")
                    or getattr(config, "openai_api_key", None)
                )
            )
            if not resolved_key or not str(resolved_key).strip():
                raise ValueError(
                    "Missing OpenAI API key. Set the OPENAI_API_KEY environment variable "
                    "or configure openai_api_key."
                )
            self.api_key = str(resolved_key).strip()
            resolved_base_url = (
                base_url
                or os.environ.get("OPENAI_BASE_URL")
                or getattr(config, "openai_base_url", None)
                or self.OPENAI_DEFAULT_BASE_URL
            )
            self.base_url = str(resolved_base_url).rstrip("/")
            resolved_model = (
                model
                or os.environ.get("OPENAI_MODEL")
                or getattr(config, "openai_model", None)
                or self.OPENAI_DEFAULT_MODEL
            )
            self.model = str(resolved_model).strip()
        else:
            # Ollama local endpoint requires no API key
            resolved_key = (
                api_key
                if api_key is not None
                else (
                    os.environ.get("OLLAMA_API_KEY")
                    or getattr(config, "ollama_api_key", None)
                )
            )
            self.api_key = str(resolved_key).strip() if resolved_key else None
            raw_host = (
                base_url
                or os.environ.get("OLLAMA_HOST")
                or getattr(config, "ollama_host", None)
                or self.OLLAMA_DEFAULT_BASE_URL
            )
            clean_host = str(raw_host).rstrip("/")
            if not clean_host.endswith("/v1"):
                clean_host = f"{clean_host}/v1"
            self.base_url = clean_host
            resolved_model = (
                model
                or os.environ.get("OLLAMA_MODEL")
                or getattr(config, "ollama_model", None)
                or self.OLLAMA_DEFAULT_MODEL
            )
            self.model = str(resolved_model).strip()

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def translate(self, text: str, language_to: str, language_from: str = "en") -> str:
        if not text:
            return text
        math_code = getattr(config, "math_code", "XMATHX")
        system_prompt = build_academic_system_prompt(
            language_from=language_from,
            language_to=language_to,
            math_code=math_code,
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": self.temperature,
        }
        response = requests.post(
            self.endpoint,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                f"Unexpected {self.provider} response format: {data}"
            ) from exc
        if not isinstance(content, str):
            raise ValueError(f"Empty or non-string content from {self.provider}")
        return content.strip()


class OpenAITranslator(OpenAICompatTranslator):
    """OpenAI chat completions translator (default model: gpt-4o-mini)."""

    name = "openai"

    def __init__(self, **kwargs):
        kwargs.setdefault("provider", "openai")
        super().__init__(**kwargs)


class OllamaTranslator(OpenAICompatTranslator):
    """Ollama local OpenAI-compatible translator (default model: qwen2.5:7b)."""

    name = "ollama"

    def __init__(self, **kwargs):
        kwargs.setdefault("provider", "ollama")
        super().__init__(**kwargs)
