import subprocess
from pathlib import Path
from unittest import mock

import pytest

from mathtranslate.pdf_compiler import (
    CJK_LANGS,
    CompileResult,
    build_default_begin,
    build_language_preamble,
    compile_tex_to_pdf,
)


def test_cjk_langs_constant():
    assert CJK_LANGS == {"zh", "zh-CN", "zh-TW", "ja", "ko"}


@pytest.mark.parametrize("lang", ["zh", "zh-CN", "zh-cn", "zh-TW", "ja", "JA", "ko"])
def test_build_language_preamble_cjk(lang):
    preamble = build_language_preamble(lang)
    assert preamble == "\\usepackage{xeCJK}\n\\usepackage{amsmath}"


def test_build_language_preamble_vietnamese_win32():
    preamble = build_language_preamble("vi", platform="win32")
    assert preamble == (
        "\\usepackage{fontspec}\n"
        "\\usepackage[vietnamese]{babel}\n"
        "\\setmainfont{Times New Roman}\n"
        "\\usepackage{amsmath}"
    )
    assert "xeCJK" not in preamble


def test_build_language_preamble_vietnamese_non_windows():
    preamble = build_language_preamble("VI", platform="linux")
    assert preamble == (
        "\\usepackage{fontspec}\n"
        "\\usepackage[vietnamese]{babel}\n"
        "\\setmainfont{DejaVu Serif}\n"
        "\\usepackage{amsmath}"
    )
    assert "xeCJK" not in preamble


@pytest.mark.parametrize("lang", ["en", "fr", "de", "es"])
def test_build_language_preamble_other_languages(lang):
    preamble = build_language_preamble(lang)
    assert preamble == "\\usepackage{fontspec}\n\\usepackage{amsmath}"
    assert "xeCJK" not in preamble


def test_build_default_begin_vi_and_cjk():
    vi_begin = build_default_begin("vi", platform="win32")
    assert r"\documentclass[UTF8]{article}" in vi_begin
    assert r"\usepackage{fontspec}" in vi_begin
    assert r"\usepackage[vietnamese]{babel}" in vi_begin
    assert r"\setmainfont{Times New Roman}" in vi_begin
    assert r"\usepackage{amsmath}" in vi_begin
    assert r"\usepackage{amssymb}" in vi_begin
    assert r"\begin{document}" in vi_begin
    assert "xeCJK" not in vi_begin

    zh_begin = build_default_begin("zh-CN")
    assert r"\documentclass[UTF8]{article}" in zh_begin
    assert r"\usepackage{xeCJK}" in zh_begin
    assert r"\usepackage{amsmath}" in zh_begin
    assert r"\usepackage{amssymb}" in zh_begin
    assert r"\begin{document}" in zh_begin


def test_compile_tex_to_pdf_missing_xelatex(tmp_path):
    tex_file = tmp_path / "paper.tex"
    tex_file.write_text(r"\documentclass{article}\begin{document}Hi\end{document}", encoding="utf-8")

    with mock.patch("mathtranslate.pdf_compiler.shutil.which", return_value=None):
        with mock.patch("mathtranslate.pdf_compiler.subprocess.run") as mock_run:
            result = compile_tex_to_pdf(str(tex_file))

    assert isinstance(result, CompileResult)
    assert result.status == "missing_compiler"
    assert result.pdf_path is None
    assert "XeLaTeX not found in PATH" in result.message
    assert "Overleaf" in result.message
    mock_run.assert_not_called()


def test_compile_tex_to_pdf_success_without_bibtex(tmp_path):
    subdir = tmp_path / "thư mục có dấu cách"
    subdir.mkdir()
    tex_file = subdir / "bài báo.tex"
    tex_file.write_text(r"\documentclass{article}\begin{document}Xin chào\end{document}", encoding="utf-8")
    pdf_file = subdir / "bài báo.pdf"

    def fake_run(cmd, cwd, capture_output, text, timeout, errors):
        assert isinstance(cmd, list)
        assert cmd == ["xelatex", "-interaction=nonstopmode", "bài báo.tex"]
        assert Path(cwd) == subdir.resolve()
        assert capture_output is True
        assert text is True
        assert errors == "replace"
        pdf_file.write_bytes(b"%PDF-1.4 mock")
        return subprocess.CompletedProcess(cmd, 0, stdout="Output written on paper.pdf", stderr="")

    with mock.patch("mathtranslate.pdf_compiler.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
        with mock.patch("mathtranslate.pdf_compiler.subprocess.run", side_effect=fake_run) as mock_run:
            result = compile_tex_to_pdf(str(tex_file))

    assert result.status == "success"
    assert result.pdf_path == str(pdf_file)
    assert "Output written on paper.pdf" in result.log
    assert mock_run.call_count == 1
    for call in mock_run.call_args_list:
        assert call.kwargs.get("shell") is not True


def test_compile_tex_to_pdf_success_with_bibtex(tmp_path):
    tex_file = tmp_path / "main.tex"
    tex_file.write_text(r"\documentclass{article}\begin{document}\cite{a}\end{document}", encoding="utf-8")
    bib_file = tmp_path / "refs.bib"
    bib_file.write_text("@article{a, title={Test}}", encoding="utf-8")
    aux_file = tmp_path / "main.aux"
    pdf_file = tmp_path / "main.pdf"

    commands_called = []

    def fake_run(cmd, cwd, capture_output, text, timeout, errors):
        assert isinstance(cmd, list)
        commands_called.append(cmd)
        if cmd[0] == "xelatex":
            aux_file.write_text(r"\citation{a}", encoding="utf-8")
            if len([c for c in commands_called if c[0] == "xelatex"]) >= 3:
                pdf_file.write_bytes(b"%PDF-1.4 mock")
        return subprocess.CompletedProcess(cmd, 0, stdout=f"ran {' '.join(cmd)}", stderr="")

    with mock.patch("mathtranslate.pdf_compiler.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
        with mock.patch("mathtranslate.pdf_compiler.subprocess.run", side_effect=fake_run) as mock_run:
            result = compile_tex_to_pdf(str(tex_file), work_dir=str(tmp_path), timeout=60)

    assert result.status == "success"
    assert result.pdf_path == str(pdf_file)
    assert commands_called == [
        ["xelatex", "-interaction=nonstopmode", "main.tex"],
        ["bibtex", "main"],
        ["xelatex", "-interaction=nonstopmode", "main.tex"],
        ["xelatex", "-interaction=nonstopmode", "main.tex"],
    ]
    for call in mock_run.call_args_list:
        assert call.kwargs.get("shell") is not True
        assert call.kwargs["timeout"] == 60


def test_compile_tex_to_pdf_compile_error_no_pdf(tmp_path):
    tex_file = tmp_path / "broken.tex"
    tex_file.write_text(r"\documentclass{article}\begin{document}\badcmd\end{document}", encoding="utf-8")

    def fake_run(cmd, cwd, capture_output, text, timeout, errors):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="! Undefined control sequence.",
            stderr="fatal error",
        )

    with mock.patch("mathtranslate.pdf_compiler.shutil.which", return_value="/usr/bin/xelatex"):
        with mock.patch("mathtranslate.pdf_compiler.subprocess.run", side_effect=fake_run):
            result = compile_tex_to_pdf(str(tex_file))

    assert result.status == "compile_error"
    assert result.pdf_path is None
    assert "Undefined control sequence" in result.log
    assert "fatal error" in result.log


def test_compile_tex_to_pdf_timeout(tmp_path):
    tex_file = tmp_path / "slow.tex"
    tex_file.write_text(r"\documentclass{article}\begin{document}loop\end{document}", encoding="utf-8")

    exc = subprocess.TimeoutExpired(
        cmd=["xelatex", "-interaction=nonstopmode", "slow.tex"],
        timeout=5,
        output="partial stdout",
        stderr="partial stderr",
    )

    with mock.patch("mathtranslate.pdf_compiler.shutil.which", return_value="/usr/bin/xelatex"):
        with mock.patch("mathtranslate.pdf_compiler.subprocess.run", side_effect=exc):
            result = compile_tex_to_pdf(str(tex_file), timeout=5)

    assert result.status == "timeout"
    assert result.pdf_path is None
    assert "5 seconds" in result.message
    assert "partial stdout" in result.log
    assert "partial stderr" in result.log
