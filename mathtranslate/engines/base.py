from abc import ABC, abstractmethod
import urllib.error
import requests


LANGUAGE_NAMES = {
    "af": "Afrikaans",
    "sq": "Albanian",
    "ar": "Arabic",
    "az": "Azerbaijani",
    "eu": "Basque",
    "be": "Belarusian",
    "bn": "Bengali",
    "bg": "Bulgarian",
    "ca": "Catalan",
    "zh": "Simplified Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "hr": "Croatian",
    "cs": "Czech",
    "da": "Danish",
    "nl": "Dutch",
    "en": "English",
    "eo": "Esperanto",
    "et": "Estonian",
    "tl": "Filipino",
    "fi": "Finnish",
    "fr": "French",
    "gl": "Galician",
    "ka": "Georgian",
    "de": "German",
    "el": "Greek",
    "gu": "Gujarati",
    "ht": "Haitian Creole",
    "iw": "Hebrew",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "is": "Icelandic",
    "id": "Indonesian",
    "ga": "Irish",
    "it": "Italian",
    "ja": "Japanese",
    "kn": "Kannada",
    "ko": "Korean",
    "la": "Latin",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "mk": "Macedonian",
    "ms": "Malay",
    "mt": "Maltese",
    "no": "Norwegian",
    "fa": "Persian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sr": "Serbian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "es": "Spanish",
    "sw": "Swahili",
    "sv": "Swedish",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "cy": "Welsh",
    "yi": "Yiddish",
}


def get_language_display_name(lang_code: str) -> str:
    if not lang_code:
        return "the source language"
    normalized = lang_code.strip().lower().replace("_", "-")
    return LANGUAGE_NAMES.get(normalized, lang_code)


def build_academic_system_prompt(
    language_from: str,
    language_to: str,
    math_code: str = "XMATHX",
) -> str:
    src_name = get_language_display_name(language_from)
    tgt_name = get_language_display_name(language_to)
    return (
        f"You are an expert academic and scientific translator specializing in mathematics, "
        f"physics, computer science, and LaTeX documents. Translate the user's text from "
        f"{src_name} ({language_from}) to {tgt_name} ({language_to}) naturally, accurately, "
        f"and in formal academic style.\n"
        f"CRITICAL RULES:\n"
        f"1. Keep EVERY placeholder token matching `{math_code}...` (for example `{math_code}_0`, "
        f"`{math_code}_1_2`, `{math_code}_12`, and paragraph separator `{math_code}SEP`) EXACTLY verbatim "
        f"in its corresponding position. Never translate, alter, split, delete, or reorder `{math_code}...` tokens.\n"
        f"2. Preserve line breaks and paragraph structure.\n"
        f"3. Output ONLY the translated text without any explanations, markdown fences, or notes."
    )


class BaseTranslator(ABC):
    """Abstract base class for all MathTranslate v2 translation engines."""

    name: str = "base"
    supports_multithread: bool = True

    @abstractmethod
    def translate(self, text: str, language_to: str, language_from: str) -> str:
        """Translate `text` from `language_from` to `language_to`."""
        raise NotImplementedError

    def is_error_request_frequency(self, exc: BaseException) -> bool:
        """Return True if `exc` indicates a rate-limit / request-frequency error."""
        if isinstance(exc, requests.HTTPError) and getattr(exc, "response", None) is not None:
            if getattr(exc.response, "status_code", None) == 429:
                return True
        if isinstance(exc, urllib.error.HTTPError) and getattr(exc, "code", None) == 429:
            return True
        return False

    def is_retryable_error(self, exc: BaseException) -> bool:
        """Return True if `exc` is a transient network, rate-limit, or server error."""
        if isinstance(exc, ValueError):
            return False
        if self.is_error_request_frequency(exc):
            return True
        if isinstance(exc, requests.HTTPError) and getattr(exc, "response", None) is not None:
            status = getattr(exc.response, "status_code", None)
            if status in (400, 401, 403, 404):
                return False
            if status in (408, 429, 500, 502, 503, 504):
                return True
        if isinstance(exc, urllib.error.HTTPError):
            status = getattr(exc, "code", None)
            if status in (400, 401, 403, 404):
                return False
            if status in (408, 429, 500, 502, 503, 504):
                return True
        if isinstance(
            exc,
            (
                requests.RequestException,
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
            ),
        ):
            return True
        return False
