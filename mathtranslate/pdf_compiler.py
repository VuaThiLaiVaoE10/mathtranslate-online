from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CJK_LANGS = {"zh", "zh-CN", "zh-TW", "ja", "ko"}
_CJK_LANGS_LOWER = {lang.lower() for lang in CJK_LANGS}

CompileStatus = Literal["success", "missing_compiler", "compile_error", "timeout"]

_MISSING_COMPILER_MESSAGE = (
    "XeLaTeX not found in PATH. Install MiKTeX or TeX Live, or upload the "
    "output to Overleaf (set Compiler to XeLaTeX)."
)


@dataclass
class CompileResult:
    """Result of compiling a LaTeX source file to PDF via XeLaTeX."""

    status: CompileStatus
    pdf_path: str | None
    log: str
    message: str


def build_language_preamble(l_to: str, platform: str | None = None) -> str:
    """Return the XeLaTeX preamble packages appropriate for target language ``l_to``."""
    lang = (l_to or "").strip().lower()
    if lang in _CJK_LANGS_LOWER:
        return "\\usepackage{xeCJK}\n\\usepackage{amsmath}"

    if lang == "vi":
        target_platform = (sys.platform if platform is None else platform).lower()
        font_name = "Times New Roman" if target_platform in ("win32", "darwin") else "DejaVu Serif"
        return (
            "\\usepackage{fontspec}\n"
            "\\usepackage[bidi=default]{babel}\n"
            "\\babelprovide[import,main]{vietnamese}\n"
            f"\\setmainfont{{{font_name}}}\n"
            "\\usepackage{amsmath}"
        )

    return "\\usepackage{fontspec}\n\\usepackage{amsmath}"


def build_default_begin(l_to: str, platform: str | None = None) -> str:
    """Build a complete default LaTeX document header for non-complete fragments."""
    preamble = build_language_preamble(l_to, platform=platform)
    return "\n".join(
        [
            r"\documentclass[UTF8]{article}",
            preamble,
            r"\usepackage{amssymb}",
            r"\begin{document}",
        ]
    ) + "\n"


def _coerce_output(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _format_step_log(cmd: list[str], stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts = [f"$ {' '.join(cmd)}"]
    out = _coerce_output(stdout).strip()
    err = _coerce_output(stderr).strip()
    if out:
        parts.append(out)
    if err:
        parts.append(err)
    return "\n".join(parts)


def _resolve_tex_arg(tex_path_obj: Path, work_dir_path: Path) -> str:
    """Resolve the .tex argument passed to xelatex relative to ``work_dir_path``."""
    if tex_path_obj.parent == Path("."):
        return tex_path_obj.name
    try:
        rel = os.path.relpath(os.path.abspath(tex_path_obj), os.path.abspath(work_dir_path))
    except ValueError:
        return tex_path_obj.name
    if rel.startswith(".."):
        return tex_path_obj.name
    return Path(rel).as_posix()


def compile_tex_to_pdf(
    tex_path: str,
    work_dir: str | None = None,
    timeout: int = 180,
) -> CompileResult:
    """Compile ``tex_path`` to PDF with XeLaTeX (and BibTeX when applicable).

    Uses list-form ``subprocess.run`` without shell invocation so paths
    containing spaces or Unicode characters (e.g. Vietnamese) are handled safely.
    """
    if shutil.which("xelatex") is None:
        return CompileResult(
            status="missing_compiler",
            pdf_path=None,
            log="",
            message=_MISSING_COMPILER_MESSAGE,
        )

    tex_path_obj = Path(tex_path)
    if work_dir is None:
        work_dir_path = tex_path_obj.resolve().parent
        tex_filename = tex_path_obj.name
    else:
        work_dir_path = Path(work_dir)
        tex_filename = _resolve_tex_arg(tex_path_obj, work_dir_path)

    cwd = str(work_dir_path)
    stem = tex_path_obj.stem
    xelatex_cmd = ["xelatex", "-interaction=nonstopmode", tex_filename]

    log_chunks: list[str] = []

    def _run_step(cmd: list[str]) -> None:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        log_chunks.append(_format_step_log(cmd, proc.stdout, proc.stderr))

    try:
        _run_step(xelatex_cmd)

        aux_exists = (work_dir_path / f"{stem}.aux").exists()
        bib_exists = any(work_dir_path.glob("*.bib"))
        if shutil.which("bibtex") is not None and aux_exists and bib_exists:
            _run_step(["bibtex", stem])
            _run_step(xelatex_cmd)
            _run_step(xelatex_cmd)
        elif aux_exists:
            _run_step(xelatex_cmd)
    except subprocess.TimeoutExpired as exc:
        log_chunks.append(_format_step_log(list(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else xelatex_cmd, exc.stdout, exc.stderr))
        return CompileResult(
            status="timeout",
            pdf_path=None,
            log="\n\n".join(log_chunks),
            message=f"XeLaTeX compilation timed out after {timeout} seconds.",
        )
    except FileNotFoundError:
        return CompileResult(
            status="missing_compiler",
            pdf_path=None,
            log="\n\n".join(log_chunks),
            message=_MISSING_COMPILER_MESSAGE,
        )
    except OSError as exc:
        log_chunks.append(str(exc))
        return CompileResult(
            status="compile_error",
            pdf_path=None,
            log="\n\n".join(log_chunks),
            message=f"Failed to invoke XeLaTeX: {exc}",
        )

    full_log = "\n\n".join(log_chunks)
    pdf_file = work_dir_path / f"{stem}.pdf"
    if pdf_file.exists():
        return CompileResult(
            status="success",
            pdf_path=str(pdf_file),
            log=full_log,
            message=f"Compiled PDF successfully: {pdf_file}",
        )

    return CompileResult(
        status="compile_error",
        pdf_path=None,
        log=full_log,
        message="XeLaTeX compilation failed: PDF output was not produced.",
    )


def compile_zip_to_pdf(
    zip_path: str,
    output_pdf_path: str,
    timeout: int = 180,
) -> CompileResult:
    """Safely extract a translated .zip archive and compile its main .tex file to PDF."""
    import tempfile
    import zipfile
    from .translate_arxiv import loop_files, safe_extract_zip

    if not os.path.isfile(zip_path) or not zipfile.is_zipfile(zip_path):
        return CompileResult(
            status="compile_error",
            pdf_path=None,
            log="",
            message="Translated ZIP archive not found for PDF compilation.",
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        safe_extract_zip(zip_path, temp_dir)
        tex_candidates = sorted(f for f in loop_files(temp_dir) if f.lower().endswith(".tex"))
        if not tex_candidates:
            return CompileResult(
                status="compile_error",
                pdf_path=None,
                log="",
                message="No .tex file found inside the translated archive for PDF compilation.",
            )
        main_tex = next(
            (f for f in tex_candidates if os.path.basename(f).lower() == "main.tex"),
            tex_candidates[0],
        )
        res = compile_tex_to_pdf(main_tex, work_dir=os.path.dirname(main_tex), timeout=timeout)
        if res.status == "success" and res.pdf_path and os.path.isfile(res.pdf_path):
            out_dir = os.path.dirname(os.path.abspath(output_pdf_path))
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            shutil.copyfile(res.pdf_path, output_pdf_path)
            return CompileResult(
                status="success",
                pdf_path=output_pdf_path,
                log=res.log,
                message=f"Compiled PDF successfully: {output_pdf_path}",
            )
        return res
