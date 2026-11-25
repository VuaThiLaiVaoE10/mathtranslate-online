from typing import Optional
import mtranslate
import requests

from .base import BaseTranslator


class GoogleTranslator(BaseTranslator):
    """Google Translate engine using translate.googleapis.com with mtranslate fallback."""

    name = "google"
    supports_multithread = True
    ENDPOINT = "https://translate.googleapis.com/translate_a/single"
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    def __init__(self, timeout: int = 15, user_agent: Optional[str] = None, **_kwargs):
        self.timeout = timeout
        self.user_agent = user_agent or self.DEFAULT_USER_AGENT

    def _translate_via_googleapis(self, text: str, language_to: str, language_from: str) -> str:
        params = {
            "client": "gtx",
            "sl": language_from or "auto",
            "tl": language_to,
            "dt": "t",
            "q": text,
        }
        headers = {"User-Agent": self.user_agent}
        response = requests.get(
            self.ENDPOINT,
            params=params,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            raise ValueError("Unexpected response structure from translate.googleapis.com")
        segments = []
        for item in data[0]:
            if isinstance(item, list) and item and item[0] is not None:
                segments.append(str(item[0]))
        if not segments and text.strip():
            raise ValueError("Empty translation segments from translate.googleapis.com")
        return "".join(segments)

    def _translate_via_mtranslate(self, text: str, language_to: str, language_from: str) -> str:
        return mtranslate.translate(text, language_to, language_from or "auto")

    def translate(self, text: str, language_to: str, language_from: str = "auto") -> str:
        if not text:
            return text
        try:
            return self._translate_via_googleapis(text, language_to, language_from)
        except Exception as primary_exc:
            try:
                return self._translate_via_mtranslate(text, language_to, language_from)
            except Exception as fallback_exc:
                raise fallback_exc from primary_exc
