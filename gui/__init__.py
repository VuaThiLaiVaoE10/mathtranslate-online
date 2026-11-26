"""Desktop GUI package for MathTranslate v2."""

from .app import MathTranslateApp, run_gui
from .controller import (
    GuiPreferences,
    PdfTranslationController,
    QueueWriter,
    TranslationTaskRunner,
    normalize_arxiv_id,
    parse_language_options,
)

__all__ = [
    "GuiPreferences",
    "MathTranslateApp",
    "PdfTranslationController",
    "QueueWriter",
    "TranslationTaskRunner",
    "normalize_arxiv_id",
    "parse_language_options",
    "run_gui",
]
