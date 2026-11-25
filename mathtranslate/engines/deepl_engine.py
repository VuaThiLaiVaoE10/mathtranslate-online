import os
from typing import Optional
import requests

from ..config import config
from .base import BaseTranslator


DEEPL_TARGET_LANG_MAP = {
    "en": "EN",
    "en-us": "EN-US",
    "en-gb": "EN-GB",
    "vi": "VI",
    "zh": "ZH-HANS",
    "zh-cn": "ZH-HANS",
    "zh-hans": "ZH-HANS",
    "zh-tw": "ZH-HANT",
    "zh-hant": "ZH-HANT",
    "pt": "PT",
    "pt-br": "PT-BR",
    "pt-pt": "PT-PT",
    "no": "NB",
    "iw": "HE",
    "he": "HE",
}

DEEPL_SOURCE_LANG_MAP = {
    "en": "EN",
    "en-us": "EN",
    "en-gb": "EN",
    "vi": "VI",
    "zh": "ZH",
    "zh-cn": "ZH-HANS",
    "zh-hans": "ZH-HANS",
    "zh-tw": "ZH-HANT",
    "zh-hant": "ZH-HANT",
    "pt": "PT",
    "pt-br": "PT",
    "pt-pt": "PT",
    "no": "NB",
    "iw": "HE",
    "he": "HE",
}


def normalize_deepl_language(lang_code: str, is_target: bool = True) -> str:
    """Normalize MathTranslate language code (e.g. 'en', 'vi', 'zh-CN') for DeepL API."""
    if not lang_code:
        return ""
    cleaned = lang_code.strip().lower().replace("_", "-")
    mapping = DEEPL_TARGET_LANG_MAP if is_target else DEEPL_SOURCE_LANG_MAP
    if cleaned in mapping:
        return mapping[cleaned]
    return cleaned.upper()


class DeepLTranslator(BaseTranslator):
    """DeepL REST API v2 translator supporting both Free (:fx) and Pro keys."""

    name = "deepl"
    supports_multithread = True
    FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
    PRO_ENDPOINT = "https://api.deepl.com/v2/translate"

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: int = 30,
        **_kwargs,
    ):
        resolved_key = (
            api_key
            if api_key is not None
            else (
                os.environ.get("DEEPL_API_KEY")
                or getattr(config, "deepl_api_key", None)
            )
        )
        if not resolved_key or not str(resolved_key).strip():
            raise ValueError(
                "Missing DeepL API key. Set the DEEPL_API_KEY environment variable "
                "or configure deepl_api_key."
            )
        self.api_key = str(resolved_key).strip()
        self.is_free_key = self.api_key.endswith(":fx")
        if endpoint:
            self.endpoint = endpoint.rstrip("/")
        else:
            self.endpoint = self.FREE_ENDPOINT if self.is_free_key else self.PRO_ENDPOINT
        self.timeout = timeout

    def translate(self, text: str, language_to: str, language_from: str = "en") -> str:
        if not text:
            return text
        headers = {
            "Authorization": f"DeepL-Auth-Key {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "text": [text],
            "target_lang": normalize_deepl_language(language_to, is_target=True),
        }
        if language_from and language_from.strip().lower() != "auto":
            payload["source_lang"] = normalize_deepl_language(
                language_from, is_target=False
            )

        response = requests.post(
            self.endpoint,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        try:
            translations = data["translations"]
            result = "".join(item["text"] for item in translations)
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected DeepL response format: {data}") from exc
        return result
