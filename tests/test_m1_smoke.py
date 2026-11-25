import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import mathtranslate
from mathtranslate.config import Config
from mathtranslate.translate import LatexTranslator, TextTranslator, translate_single_tex_file
from mathtranslate import utils


REPO_ROOT = Path(__file__).resolve().parents[2]
REF_TEST_LATEX = REPO_ROOT / "reference" / "test_latex"


class MockTranslator:
    """Deterministic offline translator that preserves XMATHX placeholders."""

    def __init__(self, engine="mock", language_to="vi", language_from="en"):
        self.engine = engine
        self.language_to = language_to
        self.language_from = language_from
        self.number_of_calls = 0
        self.tot_char = 0

    def translate(self, text):
        self.number_of_calls += 1
        self.tot_char += len(text)
        return f"[VI] {text}"


def test_default_language_is_vi_and_multilang_preserved():
    assert Config.default_language_from_default == "en"
    assert Config.default_language_to_default == "vi"
    assert "Vietnamese" in utils.language_list
    assert "Chinese_Simplified" in utils.language_list
    assert "Japanese" in utils.language_list


def test_google_translator_initializes_without_attribute_error():
    t = TextTranslator("google", "vi", "en")
    assert hasattr(t, "translator")
    assert callable(t.try_translate)


def test_check_update_offline_does_not_crash():
    with mock.patch("mathtranslate.utils.get_latest_version", side_effect=OSError("offline")):
        utils.check_update(require_updated=True)


def test_translate_full_latex_preserves_math_objects(tmp_path):
    sample_tex = r"""\documentclass{article}
\begin{document}
\title{Energy and Mass}
Einstein showed that energy equals $E = mc^2$ in special relativity.

\begin{equation}
\int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}
\end{equation}

This equation is fundamental in physics.
\end{document}
"""
    translator = LatexTranslator(MockTranslator(), debug=False, threads=1)
    result = translator.translate_full_latex(sample_tex, nocache=True)

    assert "$E = mc^2$" in result
    assert r"\int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}" in result
    assert "[VI]" in result
    assert translator.nbad == 0


@pytest.mark.parametrize("name", ["be", "caption", "brace", "accent", "mularg", "bibnote"])
def test_reference_test_latex_inputs_preserve_all_objects(name):
    input_file = REF_TEST_LATEX / f"input_{name}.tex"
    assert input_file.exists(), f"Missing reference test file: {input_file}"

    text = input_file.read_text(encoding="utf-8")
    translator = LatexTranslator(MockTranslator(), debug=False, threads=1)
    result = translator.translate_full_latex(text, nocache=True)

    assert len(result) > 0
    assert translator.nbad == 0


def test_cli_help_commands_run_cleanly():
    scripts_dir = Path(sys.executable).parent
    for cmd in ("translate_tex.exe", "translate_arxiv.exe"):
        exe_path = scripts_dir / cmd
        assert exe_path.exists(), f"Missing console script: {exe_path}"
        proc = subprocess.run(
            [str(exe_path), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
        assert "usage:" in proc.stdout.lower()
