import threading
import time
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
    _rate_lock = threading.Lock()
    _last_request_ts = 0.0

    def __init__(
        self,
        timeout: int = 15,
        user_agent: Optional[str] = None,
        min_interval: float = 0.15,
        **_kwargs,
    ):
        self.timeout = timeout
        self.user_agent = user_agent or self.DEFAULT_USER_AGENT
        self.min_interval = min_interval

    def _pace_request(self) -> None:
        if self.min_interval <= 0:
            return
        with GoogleTranslator._rate_lock:
            now = time.monotonic()
            wait_s = self.min_interval - (now - GoogleTranslator._last_request_ts)
            if wait_s > 0:
                time.sleep(wait_s)
            GoogleTranslator._last_request_ts = time.monotonic()

    def _translate_via_googleapis(self, text: str, language_to: str, language_from: str) -> str:
        headers = {"User-Agent": self.user_agent}
        base_params = {
            "client": "gtx",
            "sl": language_from or "auto",
            "tl": language_to,
            "dt": "t",
        }
        self._pace_request()
        if len(text) > 1500:
            response = requests.post(
                self.ENDPOINT,
                params=base_params,
                data={"q": text},
                headers=headers,
                timeout=self.timeout,
            )
        else:
            params = dict(base_params, q=text)
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
