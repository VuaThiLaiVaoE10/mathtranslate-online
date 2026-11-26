import os
import threading
import time
from typing import Optional
import requests

from ..config import config
from .base import BaseTranslator, build_academic_system_prompt


class GeminiTranslator(BaseTranslator):
    """Google Gemini REST API translator (default model: gemini-2.5-flash with 404 auto-fallback)."""

    name = "gemini"
    supports_multithread = True
    DEFAULT_MODEL = "gemini-2.5-flash"
    FALLBACK_MODELS = (
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
    )
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    _rate_lock = threading.Lock()
    _last_request_ts = 0.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        temperature: float = 0.1,
        min_interval: float = 0.1,
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
        self.min_interval = min_interval

    def _pace_request(self) -> None:
        if self.min_interval <= 0:
            return
        with GeminiTranslator._rate_lock:
            now = time.monotonic()
            wait_s = self.min_interval - (now - GeminiTranslator._last_request_ts)
            if wait_s > 0:
                time.sleep(wait_s)
            GeminiTranslator._last_request_ts = time.monotonic()

    def _discover_available_flash_model(self, headers: dict) -> Optional[str]:
        try:
            resp = requests.get(
                self.base_url,
                headers={"x-goog-api-key": headers["x-goog-api-key"]},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            models = resp.json().get("models") or []
            candidates: list[str] = []
            for m in models:
                methods = m.get("supportedGenerationMethods") or []
                raw_name = str(m.get("name") or "")
                short_name = raw_name.split("/")[-1]
                if "generateContent" in methods and short_name.startswith("gemini"):
                    candidates.append(short_name)
            for preferred in candidates:
                if "flash" in preferred and "tts" not in preferred and "image" not in preferred:
                    return preferred
            return candidates[0] if candidates else None
        except Exception:
            return None

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
        gen_cfg: dict = {
            "temperature": self.temperature,
        }
        if "2.5" in self.model:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
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
            "generationConfig": gen_cfg,
        }
        self._pace_request()
        response = requests.post(
            self.endpoint,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 400 and "thinkingConfig" in gen_cfg:
            gen_cfg.pop("thinkingConfig", None)
            self._pace_request()
            response = requests.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        if response.status_code == 404:
            tried = {self.model}
            for candidate in self.FALLBACK_MODELS:
                if candidate in tried:
                    continue
                tried.add(candidate)
                self.model = candidate
                self._pace_request()
                response = requests.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code != 404:
                    break
            if response.status_code == 404:
                discovered = self._discover_available_flash_model(headers)
                if discovered and discovered not in tried:
                    self.model = discovered
                    self._pace_request()
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
