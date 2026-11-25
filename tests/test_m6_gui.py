"""Tests for Milestone 6: Desktop GUI and headless controller."""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from unittest import mock

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from gui.app import MathTranslateApp
from gui.controller import (
    GuiPreferences,
    QueueWriter,
    TranslationTaskRunner,
    normalize_arxiv_id,
    parse_language_options,
)
from mathtranslate import config as config_module
from mathtranslate.engines import AVAILABLE_ENGINES
from mathtranslate.pdf_compiler import CompileResult


def _drain_queue(q: queue.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


def test_queue_writer_captures_stdout_stderr_and_restores_streams():
    log_q: queue.Queue[str] = queue.Queue()
    orig_stdout = sys.stdout
    orig_stderr = sys.stderr

    with QueueWriter(log_q) as writer:
        assert sys.stdout is writer
        assert sys.stderr is writer
        print("hello stdout")
        print("hello stderr", file=sys.stderr)
        writer.write("partial ")
        writer.write("line\r\nprogress 10%\rprogress 100%\n")
        writer.write("unterminated")

    assert sys.stdout is orig_stdout
    assert sys.stderr is orig_stderr

    lines = _drain_queue(log_q)
    assert lines == [
        "hello stdout",
        "hello stderr",
        "partial line",
        "progress 100%",
        "unterminated",
    ]


def test_queue_writer_restores_streams_on_exception():
    log_q: queue.Queue[str] = queue.Queue()
    orig_stdout = sys.stdout
    orig_stderr = sys.stderr

    with pytest.raises(RuntimeError, match="boom"):
        with QueueWriter(log_q):
            print("before error")
            raise RuntimeError("boom")

    assert sys.stdout is orig_stdout
    assert sys.stderr is orig_stderr
    assert _drain_queue(log_q) == ["before error"]


def test_parse_language_options_has_required_languages_and_count():
    options = parse_language_options()
    assert len(options) >= 60

    codes = {code for _, code in options}
    assert "vi" in codes
    assert "en" in codes
    assert "zh-CN" in codes

    names = [name for name, _ in options]
    assert names == sorted(names, key=str.lower)
    assert ("Vietnamese", "vi") in options
    assert ("English", "en") in options
    assert ("Chinese Simplified", "zh-CN") in options


def test_gui_preferences_load_and_save_isolated(monkeypatch, tmp_path):
    temp_default_dir = tmp_path / "default"
    temp_default_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_module, "default_dir", str(temp_default_dir))

    prefs = GuiPreferences.load()
    assert prefs.l_from == "en"
    assert prefs.l_to == "vi"

    commands_path = tmp_path / "my commands" / "cmds.json"
    prefs.engine = "gemini"
    prefs.l_from = "fr"
    prefs.l_to = "vi"
    prefs.threads = 4
    prefs.compile_pdf = True
    prefs.commands_file = str(commands_path)
    prefs.save()

    reloaded = GuiPreferences.load()
    assert reloaded.engine == "gemini"
    assert reloaded.l_from == "fr"
    assert reloaded.l_to == "vi"
    assert reloaded.threads == 4
    assert reloaded.compile_pdf is True
    assert reloaded.commands_file == str(commands_path)


def test_normalize_arxiv_id():
    assert normalize_arxiv_id("2205.15510") == "2205.15510"
    assert normalize_arxiv_id("https://arxiv.org/abs/2205.15510v2") == "2205.15510v2"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2205.15510.pdf") == "2205.15510"


def test_runner_tex_mode_background_logs_and_callback(tmp_path):
    in_tex = tmp_path / "paper.tex"
    out_tex = tmp_path / "out" / "paper_vi.tex"
    in_tex.write_text(r"\documentclass{article}\begin{document}Hi\end{document}", encoding="utf-8")

    worker_threads: list[int] = []
    done_event = threading.Event()
    results: list[tuple[bool, str]] = []

    def fake_translate_tex(inp, outp, engine, l_from, l_to, debug, nocache, threads):
        worker_threads.append(threading.get_ident())
        print(f"translating {inp} -> {outp} ({engine} {l_from}->{l_to})")
        Path(outp).write_text("translated", encoding="utf-8")

    def fake_compile(tex_path, work_dir=None, timeout=180):
        pdf_path = Path(tex_path).with_suffix(".pdf")
        pdf_path.write_bytes(b"%PDF-1.4")
        return CompileResult(
            status="success",
            pdf_path=str(pdf_path),
            log="ok",
            message=f"Compiled PDF successfully: {pdf_path}",
        )

    def on_done(ok: bool, msg: str) -> None:
        results.append((ok, msg))
        done_event.set()

    runner = TranslationTaskRunner(on_complete=on_done)
    prefs = GuiPreferences(engine="google", l_from="en", l_to="vi", threads=2, compile_pdf=True)

    with (
        mock.patch("gui.controller.translate_single_tex_file", side_effect=fake_translate_tex) as m_tex,
        mock.patch("gui.controller.compile_tex_to_pdf", side_effect=fake_compile) as m_pdf,
    ):
        runner.start("tex", str(in_tex), str(out_tex), prefs=prefs)
        assert done_event.wait(timeout=5)
        runner.wait(timeout=5)

    assert m_tex.called
    assert m_pdf.called
    assert worker_threads and worker_threads[0] != threading.get_ident()
    assert results and results[0][0] is True
    assert str(out_tex.with_suffix(".pdf")) in results[0][1]

    logs = "\n".join(_drain_queue(runner.log_queue))
    assert "translating" in logs
    assert "PDF saved to" in logs
    statuses = _drain_queue(runner.status_queue)
    assert statuses == results


def test_runner_zip_mode_success_and_failure(tmp_path):
    in_zip = tmp_path / "proj.zip"
    out_zip = tmp_path / "proj_vi.zip"
    in_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    # Success path
    done_ok = threading.Event()
    ok_results: list[tuple[bool, str]] = []
    runner = TranslationTaskRunner(
        on_complete=lambda ok, msg: (ok_results.append((ok, msg)), done_ok.set())
    )

    def fake_zip_ok(src, dst, engine, l_from, l_to, debug=False, nocache=False, threads=0):
        print(f"zip ok: {src} -> {dst}")
        return True

    with mock.patch("gui.controller.translate_zip_project", side_effect=fake_zip_ok) as m_zip:
        runner.start("zip", str(in_zip), str(out_zip), prefs=GuiPreferences())
        assert done_ok.wait(timeout=5)
        runner.wait(timeout=5)

    assert m_zip.called
    assert ok_results[0][0] is True
    assert "zip ok" in "\n".join(_drain_queue(runner.log_queue))

    # Failure path (no complete .tex)
    done_fail = threading.Event()
    fail_results: list[tuple[bool, str]] = []
    runner_fail = TranslationTaskRunner(
        on_complete=lambda ok, msg: (fail_results.append((ok, msg)), done_fail.set())
    )
    with mock.patch("gui.controller.translate_zip_project", return_value=False):
        runner_fail.start("zip", str(in_zip), str(out_zip), prefs=GuiPreferences())
        assert done_fail.wait(timeout=5)
        runner_fail.wait(timeout=5)

    assert fail_results[0][0] is False


def test_runner_arxiv_mode_background_execution(tmp_path):
    out_zip = tmp_path / "2205.15510.zip"
    done_event = threading.Event()
    results: list[tuple[bool, str]] = []

    def fake_arxiv_main(args=None, require_updated=False):
        print("arxiv args:", " ".join(args or []))
        return True

    runner = TranslationTaskRunner(
        on_complete=lambda ok, msg: (results.append((ok, msg)), done_event.set())
    )
    prefs = GuiPreferences(engine="openai", l_from="en", l_to="vi", threads=3, compile_pdf=True)

    with mock.patch("gui.controller.translate_arxiv_main", side_effect=fake_arxiv_main) as m_arxiv:
        runner.start("arxiv", "https://arxiv.org/abs/2205.15510", str(out_zip), prefs=prefs)
        assert done_event.wait(timeout=5)
        runner.wait(timeout=5)

    assert m_arxiv.called
    called_args = m_arxiv.call_args.kwargs["args"]
    assert called_args[0] == "2205.15510"
    assert "-engine" in called_args and "openai" in called_args
    assert "--compile" in called_args
    assert results[0][0] is True
    assert "arxiv args:" in "\n".join(_drain_queue(runner.log_queue))


def test_mathtranslate_app_instantiates_and_destroys_cleanly():
    try:
        app = MathTranslateApp()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")

    try:
        tab_titles = [app.notebook.tab(tab_id, "text") for tab_id in app.notebook.tabs()]
        assert tab_titles == ["ArXiv", "File .tex", "Project .zip", "Cài đặt"]
        assert tuple(app.engine_combo["values"]) == AVAILABLE_ENGINES
        assert "(en)" in app.from_lang_var.get()
        assert "(vi)" in app.to_lang_var.get()

        # Verify queue draining updates ScrolledText safely on the UI thread
        app.log_queue.put("Dòng log tiếng Việt có dấu")
        app.status_queue.put((True, "Xong"))
        app._poll_queues()
        content = app.log_text.get("1.0", tk.END)
        assert "Dòng log tiếng Việt có dấu" in content
        assert "[Hoàn tất] Xong" in content
    finally:
        app.destroy()
