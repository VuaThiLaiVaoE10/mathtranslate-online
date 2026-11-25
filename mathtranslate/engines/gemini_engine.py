import os
from typing import Optional
import requests

from ..config import config
from .base import BaseTranslator, build_academic_system_prompt


class GeminiTranslator(BaseTranslator):
    """Google Gemini REST API translator (default model: gemini-2.0-flash)."""

    name = "gemini"
    supports_multithread = True
    DEFAULT_MODEL = "gemini-2.0-flash"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        temperature: float = 0.1,
        **_kwargs,
    ):
        resolved_key = (
            api_key
            if api_key is not None
            else (
                os.environ.get("GEMINI_API_KEY")
                or getattr(config, "gemini_api_key", None)
            )
        )
        if not resolved_key or not str(resolved_key).strip():
            raise ValueError(
                "Missing Gemini API key. Set the GEMINI_API_KEY environment variable "
                "or configure gemini_api_key."
            )
        self.api_key = str(resolved_key).strip()
        resolved_model = (
            model
            or os.environ.get("GEMINI_MODEL")
            or getattr(config, "gemini_model", None)
            or self.DEFAULT_MODEL
        )
        self.model = str(resolved_model).strip()
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout
        self.temperature = temperature

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/{self.model}:generateContent"

    def translate(self, text: str, language_to: str, language_from: str = "en") -> str:
        if not text:
            return text
        math_code = getattr(config, "math_code", "XMATHX")
        system_prompt = build_academic_system_prompt(
            language_from=language_from,
            language_to=language_to,
            math_code=math_code,
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        payload = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": text}],
                }
            ],
            "generationConfig": {
                "temperature": self.temperature,
            },
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
            parts = data["candidates"][0]["content"]["parts"]
            translated = "".join(
                part.get("text", "") for part in parts if isinstance(part, dict)
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected Gemini response format: {data}") from exc
        if not translated and text.strip():
            raise ValueError(f"Empty translation returned by Gemini: {data}")
        return translated.strip()
