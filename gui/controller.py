"""Headless-testable controller logic for the MathTranslate Desktop GUI."""

from __future__ import annotations

import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

from mathtranslate import config as config_module
from mathtranslate import utils
from mathtranslate.config import config
from mathtranslate.engines import AVAILABLE_ENGINES
from mathtranslate.pdf_batch import (
    BatchCheckpoint,
    PdfBatchTranslator,
    ScanPagesDetectedError,
    inspect_pdf as _inspect_pdf,
)
from mathtranslate.pdf_compiler import compile_tex_to_pdf, compile_zip_to_pdf
from mathtranslate.translate import translate_single_tex_file
from mathtranslate.translate_arxiv import (
    main as translate_arxiv_main,
    translate_zip_project,
)
from mathtranslate.utils import normalize_arxiv_id, parse_language_options

COMPILE_PDF_KEY = "DEFAULT_COMPILE_PDF"
COMMANDS_FILE_KEY = "DEFAULT_COMMANDS_FILE"


_UNSET = object()


class QueueWriter:
    """Thread-safe file-like stream writer that pushes lines into a ``queue.Queue``.

    Can also be used as a context manager to redirect both ``sys.stdout`` and
    ``sys.stderr`` safely and restore the original streams on exit.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, log_queue: queue.Queue, stream_name: str = "stdout") -> None:
        self.log_queue = log_queue
        self.stream_name = stream_name
        self._lock = threading.Lock()
        self._buffer = ""
        self._prev_stdout: object = _UNSET
        self._prev_stderr: object = _UNSET

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def _emit_line(self, line: str) -> None:
        # Collapse carriage-return progress updates (e.g., tqdm) to the latest segment.
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        self.log_queue.put(line)

    def write(self, text: str) -> int:
        if not text:
            return 0
        data = str(text).replace("\r\n", "\n")
        with self._lock:
            self._buffer += data
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._emit_line(line)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._buffer:
                self._emit_line(self._buffer)
                self._buffer = ""

    def __enter__(self) -> "QueueWriter":
        self._prev_stdout = sys.stdout
        self._prev_stderr = sys.stderr
        sys.stdout = self
        sys.stderr = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            self.flush()
        finally:
            if self._prev_stdout is not _UNSET:
                sys.stdout = self._prev_stdout  # type: ignore[assignment]
                self._prev_stdout = _UNSET
            if self._prev_stderr is not _UNSET:
                sys.stderr = self._prev_stderr  # type: ignore[assignment]
                self._prev_stderr = _UNSET


def _read_path_preserving_spaces(key: str, default: str = "") -> str:
    """Read a saved path from config dir without stripping spaces."""
    full_path = os.path.join(config_module.default_dir, key)
    if os.path.exists(full_path):
        with open(full_path, encoding="utf-8") as f:
            return f.read().strip("\r\n")
    return default


@dataclass
class GuiPreferences:
    """Persisted Desktop GUI translation preferences backed by ``mathtranslate.config.config``."""

    engine: str = "google"
    l_from: str = "en"
    l_to: str = "vi"
    threads: int = 0
    compile_pdf: bool = False
    commands_file: str = ""

    @classmethod
    def load(cls) -> "GuiPreferences":
        config.load()
        engine = (config.default_engine or "google").strip().lower()
        if engine not in AVAILABLE_ENGINES:
            engine = "google"
        l_from = (config.default_language_from or "en").strip() or "en"
        l_to = (config.default_language_to or "vi").strip() or "vi"
        try:
            threads = max(0, int(config.default_threads))
        except (TypeError, ValueError):
            threads = 0

        compile_raw = str(config.read_variable(COMPILE_PDF_KEY, "0")).strip().lower()
        compile_pdf = compile_raw in {"1", "true", "yes", "on"}
        commands_file = _read_path_preserving_spaces(COMMANDS_FILE_KEY, "")
        return cls(
            engine=engine,
            l_from=l_from,
            l_to=l_to,
            threads=threads,
            compile_pdf=compile_pdf,
            commands_file=commands_file,
        )

    def save(self) -> None:
        engine = (self.engine or "google").strip().lower()
        if engine not in AVAILABLE_ENGINES:
            raise ValueError(
                f"Unsupported engine '{self.engine}'. Available: {', '.join(AVAILABLE_ENGINES)}"
            )
        l_from = (self.l_from or "en").strip()
        l_to = (self.l_to or "vi").strip()
        threads = max(0, int(self.threads))
        compile_pdf = bool(self.compile_pdf)
        commands_file = (self.commands_file or "").strip()

        self.engine = engine
        self.l_from = l_from
        self.l_to = l_to
        self.threads = threads
        self.compile_pdf = compile_pdf
        self.commands_file = commands_file

        config.set_variable_4ui(config.default_engine_path, engine)
        config.set_variable_4ui(config.default_language_from_path, l_from)
        config.set_variable_4ui(config.default_language_to_path, l_to)
        config.set_variable_4ui(config.default_threads_path, str(threads))
        config.set_variable_4ui(COMPILE_PDF_KEY, "1" if compile_pdf else "0")
        config.set_variable_4ui(COMMANDS_FILE_KEY, commands_file)
        config.load()


def _apply_commands_file(commands_file: str) -> None:
    if commands_file:
        additional = utils.load_additional_commands(commands_file)
        config.mularg_command_list = list(config.raw_mularg_command_list) + additional
    else:
        config.mularg_command_list = list(config.raw_mularg_command_list)


def _normalize_runtime_options(
    engine: str,
    l_from: str,
    l_to: str,
    threads: int,
) -> tuple[str, str, str, int]:
    norm_engine = (engine or "google").strip().lower()
    if norm_engine not in AVAILABLE_ENGINES:
        raise ValueError(
            f"Unsupported engine '{engine}'. Available: {', '.join(AVAILABLE_ENGINES)}"
        )
    norm_from = (l_from or "en").strip()
    norm_to = (l_to or "vi").strip()
    norm_threads = max(0, int(threads))
    if norm_engine == "tencent":
        if norm_from == "zh-CN":
            norm_from = "zh"
        if norm_to == "zh-CN":
            norm_to = "zh"
        norm_threads = 1
    return norm_engine, norm_from, norm_to, norm_threads


def _compile_zip_project_to_pdf(output_zip_path: str) -> tuple[bool, str]:
    pdf_out = os.path.splitext(output_zip_path)[0] + ".pdf"
    comp_res = compile_zip_to_pdf(output_zip_path, pdf_out)
    print(comp_res.message)
    if comp_res.status == "success" and comp_res.pdf_path:
        print("PDF saved to", comp_res.pdf_path)
        return True, f"PDF saved to {comp_res.pdf_path}"
    if comp_res.log:
        print(comp_res.log)
    return False, comp_res.message


class TranslationTaskRunner:
    """Runs ArXiv, single ``.tex``, or ``.zip`` project translations in a background thread."""

    VALID_MODES = ("arxiv", "tex", "zip")

    def __init__(
        self,
        log_queue: Optional[queue.Queue] = None,
        status_queue: Optional[queue.Queue] = None,
        on_complete: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        self.log_queue: queue.Queue = log_queue if log_queue is not None else queue.Queue()
        self.status_queue: queue.Queue = status_queue if status_queue is not None else queue.Queue()
        self.on_complete = on_complete
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def start(
        self,
        mode: str,
        source: str,
        output_path: str,
        prefs: Optional[GuiPreferences] = None,
        *,
        engine: Optional[str] = None,
        l_from: Optional[str] = None,
        l_to: Optional[str] = None,
        threads: Optional[int] = None,
        compile_pdf: Optional[bool] = None,
        commands_file: Optional[str] = None,
        debug: bool = False,
        nocache: bool = False,
        on_complete: Optional[Callable[[bool, str], None]] = None,
    ) -> threading.Thread:
        norm_mode = (mode or "").strip().lower()
        if norm_mode not in self.VALID_MODES:
            raise ValueError(f"Unsupported mode '{mode}'. Expected one of {self.VALID_MODES}.")

        clean_source = normalize_arxiv_id(source) if norm_mode == "arxiv" else (source or "").strip()
        clean_output = (output_path or "").strip()
        if not clean_source:
            raise ValueError("Source input must not be empty.")
        if not clean_output:
            raise ValueError("Output path must not be empty.")

        active_prefs = prefs or GuiPreferences.load()
        eff_engine = active_prefs.engine if engine is None else engine
        eff_from = active_prefs.l_from if l_from is None else l_from
        eff_to = active_prefs.l_to if l_to is None else l_to
        eff_threads = active_prefs.threads if threads is None else threads
        eff_compile = active_prefs.compile_pdf if compile_pdf is None else bool(compile_pdf)
        eff_commands = active_prefs.commands_file if commands_file is None else commands_file
        callback = on_complete if on_complete is not None else self.on_complete

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A translation task is already running.")
            thread = threading.Thread(
                target=self._run_worker,
                args=(
                    norm_mode,
                    clean_source,
                    clean_output,
                    eff_engine,
                    eff_from,
                    eff_to,
                    eff_threads,
                    eff_compile,
                    eff_commands or "",
                    bool(debug),
                    bool(nocache),
                    callback,
                ),
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return thread

    def _emit_status(
        self,
        ok: bool,
        message: str,
        callback: Optional[Callable[[bool, str], None]],
    ) -> None:
        self.status_queue.put((ok, message))
        if callback is not None:
            try:
                callback(ok, message)
            except Exception as exc:
                self.log_queue.put(f"[GUI callback error] {exc}")

    def _run_worker(
        self,
        mode: str,
        source: str,
        output_path: str,
        engine: str,
        l_from: str,
        l_to: str,
        threads: int,
        compile_pdf: bool,
        commands_file: str,
        debug: bool,
        nocache: bool,
        callback: Optional[Callable[[bool, str], None]],
    ) -> None:
        ok = False
        message = ""
        try:
            with QueueWriter(self.log_queue):
                norm_engine, norm_from, norm_to, norm_threads = _normalize_runtime_options(
                    engine, l_from, l_to, threads
                )
                out_dir = os.path.dirname(os.path.abspath(output_path))
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)

                if mode == "tex":
                    _apply_commands_file(commands_file)
                    print(f"Start translating .tex: {source}")
                    print(f"engine={norm_engine}, {norm_from} -> {norm_to}, threads={norm_threads or 'auto'}")
                    translate_single_tex_file(
                        source,
                        output_path,
                        norm_engine,
                        norm_from,
                        norm_to,
                        debug,
                        nocache,
                        norm_threads,
                    )
                    message = f"Dịch thành công: {output_path}"
                    if compile_pdf:
                        comp_res = compile_tex_to_pdf(output_path)
                        print(comp_res.message)
                        if comp_res.status == "success" and comp_res.pdf_path:
                            print("PDF saved to", comp_res.pdf_path)
                            message = f"Dịch & biên dịch PDF thành công: {comp_res.pdf_path}"
                        else:
                            if comp_res.log:
                                print(comp_res.log)
                            message = f"Đã dịch {output_path} (PDF: {comp_res.message})"
                    ok = True

                elif mode == "zip":
                    _apply_commands_file(commands_file)
                    print(f"Start translating .zip project: {source}")
                    print(f"engine={norm_engine}, {norm_from} -> {norm_to}, threads={norm_threads or 'auto'}")
                    zip_ok = translate_zip_project(
                        source,
                        output_path,
                        norm_engine,
                        norm_from,
                        norm_to,
                        debug=debug,
                        nocache=nocache,
                        threads=norm_threads,
                    )
                    if not zip_ok:
                        ok = False
                        message = "Không tìm thấy file .tex hoàn chỉnh (\\begin{document}) trong file .zip."
                        print(message)
                    else:
                        message = f"Dịch project .zip thành công: {output_path}"
                        if compile_pdf:
                            pdf_ok, pdf_msg = _compile_zip_project_to_pdf(output_path)
                            if pdf_ok:
                                message = f"Dịch .zip & biên dịch PDF thành công: {output_path}"
                            else:
                                message = f"Đã dịch {output_path} (PDF: {pdf_msg})"
                        ok = True

                elif mode == "arxiv":
                    args = [
                        source,
                        "-o",
                        output_path,
                        "-engine",
                        norm_engine,
                        "-from",
                        norm_from,
                        "-to",
                        norm_to,
                        "-threads",
                        str(norm_threads),
                    ]
                    if commands_file:
                        args.extend(["-commands", commands_file])
                    if compile_pdf:
                        args.append("--compile")
                    if debug:
                        args.append("--debug")
                    if nocache:
                        args.append("--nocache")

                    arxiv_ok = translate_arxiv_main(args=args, require_updated=False)
                    if arxiv_ok:
                        ok = True
                        message = f"Dịch ArXiv {source} thành công: {output_path}"
                    else:
                        ok = False
                        message = f"Dịch ArXiv {source} thất bại (không có mã nguồn LaTeX hoặc lỗi tải xuống)."

        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if code == 0:
                ok = True
                message = f"Hoàn tất: {output_path}"
            else:
                ok = False
                message = "Tác vụ bị dừng (thiếu API key hoặc tham số không hợp lệ)."
                self.log_queue.put(message)
        except Exception as exc:
            ok = False
            message = f"Lỗi: {exc}"
            self.log_queue.put(f"[Error] {exc}")
        finally:
            self._emit_status(ok, message, callback)


class PdfTranslationController:
    """Headless-testable controller for layout-preserving PDF batch translation."""

    def __init__(
        self,
        log_queue: Optional[queue.Queue[str]] = None,
        status_queue: Optional[queue.Queue[tuple[bool, str]]] = None,
    ) -> None:
        self.log_queue: queue.Queue[str] = log_queue if log_queue is not None else queue.Queue()
        self.status_queue: queue.Queue[tuple[bool, str]] = (
            status_queue if status_queue is not None else queue.Queue()
        )
        self.progress_queue: queue.Queue[dict] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._batch: Optional[PdfBatchTranslator] = None
        self._work_dir: Optional[str] = None
        self.last_checkpoint: Optional[BatchCheckpoint] = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def inspect(self, input_path: str) -> dict:
        return _inspect_pdf(input_path, scan_pages_all=True)

    def inspect_pdf(self, input_path: str) -> dict:
        return self.inspect(input_path)

    def _emit_status(
        self,
        ok: bool,
        message: str,
        callback: Optional[Callable[[bool, str], None]],
    ) -> None:
        self.status_queue.put((ok, message))
        if callback is not None:
            callback(ok, message)

    def start(
        self,
        input_path: str,
        output_path: str,
        *,
        work_dir: Optional[str] = None,
        engine: str = "google",
        l_from: str = "en",
        l_to: str = "vi",
        scan_policy: str = "abort",
        glossary: Optional[dict[str, str]] = None,
        threads: int = 0,
        translator=None,
        translator_factory=None,
        on_progress: Optional[Callable] = None,
        on_complete: Optional[Callable[[bool, str], None]] = None,
    ) -> threading.Thread:
        if not input_path or not os.path.isfile(input_path):
            raise FileNotFoundError(f"Không tìm thấy file PDF đầu vào: {input_path}")
        if not output_path:
            raise ValueError("Đường dẫn file PDF đầu ra không được để trống.")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Một tác vụ dịch PDF khác đang chạy.")
            resolved_work_dir = work_dir or os.path.join(
                os.path.dirname(os.path.abspath(output_path)),
                f".pdf_work_{os.path.splitext(os.path.basename(output_path))[0]}",
            )
            os.makedirs(resolved_work_dir, exist_ok=True)
            self._work_dir = resolved_work_dir

            def _progress_hook(cp: BatchCheckpoint) -> None:
                completed = (
                    len(cp.completed_pages)
                    if isinstance(cp.completed_pages, (list, tuple, set))
                    else int(cp.completed_pages or 0)
                )
                payload = {
                    "completed_pages": completed,
                    "total_pages": int(cp.total_pages or 0),
                    "eta_seconds": cp.eta_seconds,
                    "pages_per_sec": cp.pages_per_sec,
                    "overflow_pages": list(cp.overflow_pages or []),
                    "scan_pages": list(cp.scan_pages or []),
                    "status": cp.status,
                }
                self.progress_queue.put(payload)
                if on_progress is not None:
                    on_progress(cp)

            batch = PdfBatchTranslator(
                input_path,
                output_path,
                resolved_work_dir,
                engine=engine,
                l_from=l_from,
                l_to=l_to,
                scan_policy=scan_policy,
                glossary=glossary,
                max_workers=max(1, threads) if threads else 2,
                translator=translator,
                translator_factory=translator_factory,
                on_progress=_progress_hook,
            )
            self._batch = batch

            def _worker() -> None:
                ok = False
                message = ""
                try:
                    with QueueWriter(self.log_queue):
                        cp = batch.run()
                    self.last_checkpoint = cp
                    if cp.status == "cancelled":
                        ok = False
                        message = "Đã tạm dừng dịch PDF (có thể tiếp tục từ checkpoint)."
                    elif cp.status == "completed":
                        ok = True
                        message = f"Dịch PDF hoàn tất: {output_path}"
                    else:
                        ok = False
                        message = f"Dịch PDF kết thúc với trạng thái: {cp.status}"
                except ScanPagesDetectedError as exc:
                    ok = False
                    message = str(exc)
                    self.log_queue.put(f"[Scan] {exc}")
                except Exception as exc:
                    ok = False
                    message = f"Lỗi dịch PDF: {exc}"
                    self.log_queue.put(f"[Error] {exc}")
                finally:
                    self._emit_status(ok, message, on_complete)

            thread = threading.Thread(target=_worker, daemon=True, name="mathtranslate-pdf-gui")
            self._thread = thread
            thread.start()
            return thread

    def cancel(self) -> None:
        with self._lock:
            if self._batch is not None:
                self._batch.cancel()

    def resume(
        self,
        work_dir: Optional[str] = None,
        *,
        on_progress: Optional[Callable] = None,
        on_complete: Optional[Callable[[bool, str], None]] = None,
    ) -> threading.Thread:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Một tác vụ dịch PDF khác đang chạy.")
            target_work_dir = work_dir or self._work_dir
            if self._batch is None:
                if not target_work_dir:
                    raise RuntimeError("Không có checkpoint PDF để tiếp tục.")
                self._batch = PdfBatchTranslator.from_checkpoint(
                    target_work_dir,
                    on_progress=on_progress,
                )
            batch = self._batch

            def _worker() -> None:
                ok = False
                message = ""
                try:
                    with QueueWriter(self.log_queue):
                        cp = batch.resume()
                    self.last_checkpoint = cp
                    if cp.status == "cancelled":
                        ok = False
                        message = "Đã tạm dừng dịch PDF."
                    elif cp.status == "completed":
                        ok = True
                        message = f"Tiếp tục dịch PDF hoàn tất: {batch.output_path}"
                    else:
                        ok = False
                        message = f"Dịch PDF kết thúc với trạng thái: {cp.status}"
                except Exception as exc:
                    ok = False
                    message = f"Lỗi tiếp tục PDF: {exc}"
                    self.log_queue.put(f"[Error] {exc}")
                finally:
                    self._emit_status(ok, message, on_complete)

            thread = threading.Thread(target=_worker, daemon=True, name="mathtranslate-pdf-resume")
            self._thread = thread
            thread.start()
            return thread

    def wait(self, timeout: Optional[float] = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()
