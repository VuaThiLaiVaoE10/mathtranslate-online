import argparse
import io
import json
import os
import re
import tarfile
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import pytest
import requests

from mathtranslate import process_latex, process_text, translate_arxiv, utils
from mathtranslate.config import config
from mathtranslate.translate import LatexTranslator, TextTranslator, translate_single_tex_file


class OfflineTagTranslator:
    """Deterministic offline translator preserving XMATHX tokens."""

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


def test_formatting_commands_preserved_and_inner_text_translated():
    assert process_latex.format_list == ["textbf", "textit", "emph"]

    tex = r"""\documentclass{article}
\begin{document}
This has \textbf{bold statement}, \textit{italic phrase}, and \emph{emphasized $x^2$ note}.
\end{document}
"""
    translator = LatexTranslator(OfflineTagTranslator(), debug=False, threads=1)
    result = translator.translate_full_latex(tex, nocache=True)

    assert re.search(r"\\textbf\{\[VI\] bold statement\s*\}", result)
    assert re.search(r"\\textit\{\[VI\] italic phrase\s*\}", result)
    assert re.search(r"\\emph\{\[VI\] emphasized\s+\$x\^2\$\s+note\s*\}", result)
    assert translator.nbad == 0


@pytest.mark.parametrize(
    "paragraph",
    [
        "word " * 550,                       # >2500 chars, spaces only, no periods
        "a" * 2600,                          # >2500 chars, no spaces/commas/periods
        "lowercase sentence. " * 140,        # >2500 chars, periods without uppercase next word
        "x" * process_text.char_limit,       # exact char_limit boundary
    ],
)
def test_split_too_long_paragraphs_always_terminates_under_limit(paragraph):
    result = process_text.split_too_long_paragraphs(paragraph)
    lines = result.split("\n")
    assert len(lines) >= 2
    assert all(len(line) < process_text.char_limit for line in lines)


def test_commands_safe_parser_legacy_and_json_and_rejects_code_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "mularg_command_list", list(config.raw_mularg_command_list))

    legacy_file = tmp_path / "legacy_commands.txt"
    legacy_file.write_text(
        "# comment\nadditional_commands = [\n  ('mycmd', 2, (1,)),\n]\n",
        encoding="utf-8",
    )

    parser = argparse.ArgumentParser()
    utils.add_arguments(parser)
    opts = parser.parse_args(["-commands", str(legacy_file)])
    utils.process_options(opts)
    assert ("mycmd", 2, (1,)) in config.mularg_command_list

    json_file = tmp_path / "commands.json"
    json_file.write_text(json.dumps([["jsoncmd", 3, [0, 2]]]), encoding="utf-8")
    opts_json = parser.parse_args(["-commands", str(json_file)])
    utils.process_options(opts_json)
    assert ("jsoncmd", 3, (0, 2)) in config.mularg_command_list

    pwn_marker = tmp_path / "pwned.txt"
    malicious_payloads = [
        f"__import__('pathlib').Path({str(pwn_marker)!r}).write_text('pwned')",
        f"additional_commands = __import__('pathlib').Path({str(pwn_marker)!r}).write_text('pwned')",
        "import os\nadditional_commands = [('ok', 1, (0,))]",
    ]
    for idx, payload in enumerate(malicious_payloads):
        bad_file = tmp_path / f"bad_{idx}.txt"
        bad_file.write_text(payload, encoding="utf-8")
        opts_bad = parser.parse_args(["-commands", str(bad_file)])
        with pytest.raises(ValueError):
            utils.process_options(opts_bad)
        assert not pwn_marker.exists()


def test_text_translator_retry_backoff_and_tencent_rate_limit(monkeypatch):
    sleeps = []
    monkeypatch.setattr("mathtranslate.translate.time.sleep", lambda s: sleeps.append(s))

    t = TextTranslator("google", "vi", "en")
    http_err = urllib.error.HTTPError("https://example.com", 429, "Too Many Requests", {}, None)
    t.try_translate = mock.Mock(side_effect=[http_err, TimeoutError("timed out"), "Xin chao"])

    assert t.translate("Hello world") == "Xin chao"
    assert t.try_translate.call_count == 3
    assert sleeps == [0.5, 1.0]

    # Exhausting max 3 attempts re-raises
    sleeps.clear()
    t.try_translate = mock.Mock(side_effect=requests.RequestException("network down"))
    with pytest.raises(requests.RequestException):
        t.translate("Hello again")
    assert t.try_translate.call_count == 3
    assert sleeps == [0.5, 1.0]

    # Non-transient error fails immediately without retry
    sleeps.clear()
    t.try_translate = mock.Mock(side_effect=ValueError("bad input"))
    with pytest.raises(ValueError):
        t.translate("Hello once")
    assert t.try_translate.call_count == 1
    assert sleeps == []

    # Tencent is_error_request_frequency still retries after 0.5s
    sleeps.clear()
    t.translator = mock.Mock()
    t.translator.is_error_request_frequency = mock.Mock(return_value=True)
    t.try_translate = mock.Mock(side_effect=[RuntimeError("rate limit"), "Ket qua"])
    assert t.translate("Test frequency") == "Ket qua"
    assert sleeps == [0.5]


def test_translate_single_tex_file_atomic_write(tmp_path, monkeypatch):
    inp = tmp_path / "in.tex"
    out = tmp_path / "out.tex"
    inp.write_text(r"\documentclass{article}\begin{document}Hello world.\end{document}", encoding="utf-8")
    out.write_text("PREVIOUS CONTENT", encoding="utf-8")

    monkeypatch.setattr("mathtranslate.translate.TextTranslator", OfflineTagTranslator)
    translate_single_tex_file(str(inp), str(out), "mock", "en", "vi", debug=False, nocache=True, threads=1)

    assert not (tmp_path / "out.tex.tmp").exists()
    content = out.read_text(encoding="utf-8")
    assert "[VI]" in content


def test_translate_zip_project_multifile_preserves_cwd(tmp_path, monkeypatch):
    in_zip = tmp_path / "project.zip"
    out_zip = tmp_path / "translated.zip"

    with zipfile.ZipFile(in_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n\\input{sections/intro}\n\\end{document}\n",
        )
        zf.writestr(
            "sections/intro.tex",
            "This is \\textbf{important} with $E = mc^2$.\n",
        )

    monkeypatch.setattr("mathtranslate.translate.TextTranslator", OfflineTagTranslator)
    cwd_before = os.getcwd()
    ok = translate_arxiv.translate_zip_project(
        str(in_zip),
        str(out_zip),
        engine="mock",
        l_from="en",
        l_to="vi",
        debug=False,
        nocache=True,
        threads=1,
    )
    assert os.getcwd() == cwd_before
    assert ok is True
    assert out_zip.exists()

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = zf.namelist()
        assert "main.tex" in names
        main_content = zf.read("main.tex").decode("utf-8")
        assert re.search(r"\\textbf\{\[VI\] important\s*\}", main_content)
        assert "$E = mc^2$" in main_content


def test_archive_extraction_blocks_zip_slip_and_tar_slip(tmp_path):
    bad_zip = tmp_path / "evil.zip"
    out_zip = tmp_path / "out.zip"
    escaped_target = tmp_path / "evil.txt"

    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../evil.txt", "malicious")
        zf.writestr("main.tex", r"\documentclass{article}\begin{document}Hi\end{document}")

    with pytest.raises(ValueError):
        translate_arxiv.translate_zip_project(
            str(bad_zip), str(out_zip), "google", "en", "vi", nocache=True, threads=1
        )
    assert not escaped_target.exists()
    assert not out_zip.exists()

    bad_tar = tmp_path / "evil.tar"
    extract_dir = tmp_path / "tar_dest"
    extract_dir.mkdir()
    escaped_tar_target = tmp_path / "evil_tar.txt"

    with tarfile.open(bad_tar, "w") as tf:
        data = b"malicious"
        info = tarfile.TarInfo(name="../evil_tar.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    with pytest.raises(ValueError):
        translate_arxiv.safe_extract_tar(str(bad_tar), str(extract_dir))
    assert not escaped_tar_target.exists()


def test_no_syntax_warnings_on_m2_modules():
    pkg_dir = Path(__file__).resolve().parents[1] / "mathtranslate"
    targets = [
        "process_latex.py",
        "process_text.py",
        "utils.py",
        "translate.py",
        "process_file.py",
        "translate_arxiv.py",
    ]
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        for name in targets:
            path = pkg_dir / name
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
