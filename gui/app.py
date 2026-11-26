"""Cross-platform Tkinter + ttk Desktop GUI for MathTranslate v2."""

from __future__ import annotations

import json
import os
import queue
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from mathtranslate.engines import AVAILABLE_ENGINES

from .controller import (
    GuiPreferences,
    PdfTranslationController,
    TranslationTaskRunner,
    normalize_arxiv_id,
    parse_language_options,
)


def _enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class MathTranslateApp(tk.Tk):
    """Main Tkinter Desktop window for translating ArXiv papers, .tex files, and .zip projects."""

    POLL_INTERVAL_MS = 100

    def __init__(self, runner: Optional[TranslationTaskRunner] = None) -> None:
        super().__init__()
        self.title("MathTranslate v2 — Dịch tài liệu LaTeX")
        self.geometry("820x660")
        self.minsize(720, 560)

        self._configure_theme_and_fonts()

        self.prefs = GuiPreferences.load()
        self.log_queue: queue.Queue = queue.Queue()
        self.status_queue: queue.Queue = queue.Queue()
        self.runner = runner or TranslationTaskRunner(
            log_queue=self.log_queue,
            status_queue=self.status_queue,
        )
        # Ensure we poll the queues that the injected/created runner writes to.
        self.log_queue = self.runner.log_queue
        self.status_queue = self.runner.status_queue

        self._language_options = parse_language_options()
        self._label_to_code = {f"{name} ({code})": code for name, code in self._language_options}
        self._code_to_label = {code: f"{name} ({code})" for name, code in self._language_options}
        self._language_labels = list(self._label_to_code.keys())

        # Shared translation option vars
        self.compile_pdf_var = tk.BooleanVar(value=self.prefs.compile_pdf)
        self.engine_var = tk.StringVar(value=self.prefs.engine)
        self.from_lang_var = tk.StringVar(
            value=self._code_to_label.get(self.prefs.l_from, self._code_to_label.get("en", "English (en)"))
        )
        self.to_lang_var = tk.StringVar(
            value=self._code_to_label.get(self.prefs.l_to, self._code_to_label.get("vi", "Vietnamese (vi)"))
        )
        self.threads_var = tk.StringVar(value=str(self.prefs.threads))
        self.commands_file_var = tk.StringVar(value=self.prefs.commands_file)

        # Tab input/output vars
        self.arxiv_id_var = tk.StringVar()
        self.arxiv_out_var = tk.StringVar()
        self.tex_in_var = tk.StringVar()
        self.tex_out_var = tk.StringVar()
        self.zip_in_var = tk.StringVar()
        self.zip_out_var = tk.StringVar()
        self.pdf_in_var = tk.StringVar()
        self.pdf_out_var = tk.StringVar()
        self.pdf_scan_policy_var = tk.StringVar(value="abort")
        self.pdf_progress_var = tk.StringVar(value="Tiến độ PDF: 0 / 0 trang")
        self.pdf_eta_var = tk.StringVar(value="ETA: --")
        self.pdf_overflow_var = tk.StringVar(value="Trang tràn chữ: Không có")
        self.pdf_controller = PdfTranslationController(
            log_queue=self.log_queue,
            status_queue=self.status_queue,
        )

        self.status_var = tk.StringVar(value="Sẵn sàng.")
        self._translate_buttons: list[ttk.Button] = []
        self._poll_after_id: Optional[str] = None

        self._build_ui()
        while True:
            try:
                prog = self.pdf_controller.progress_queue.get_nowait()
            except queue.Empty:
                break
            self.pdf_progress_var.set(
                f"Tiến độ PDF: {prog.get('completed_pages', 0)} / {prog.get('total_pages', 0)} trang"
            )
            eta = prog.get("eta_seconds")
            self.pdf_eta_var.set(f"ETA: {eta:.1f}s" if isinstance(eta, (int, float)) else "ETA: --")
            overflow = prog.get("overflow_pages") or []
            self.pdf_overflow_var.set(
                f"Trang tràn chữ: {', '.join(map(str, overflow))}" if overflow else "Trang tràn chữ: Không có"
            )

        self._poll_after_id = self.after(self.POLL_INTERVAL_MS, self._poll_queues)

    def _configure_theme_and_fonts(self) -> None:
        style = ttk.Style(self)
        available = style.theme_names()
        for preferred in ("vista", "clam", "aqua", "default"):
            if preferred in available:
                try:
                    style.theme_use(preferred)
                    break
                except tk.TclError:
                    continue

        ui_family = "Segoe UI" if sys.platform == "win32" else "TkDefaultFont"
        mono_family = "Consolas" if sys.platform == "win32" else "TkFixedFont"
        self._mono_font = (mono_family, 9)

        if sys.platform == "win32":
            self.option_add("*Font", (ui_family, 10))
            style.configure(".", font=(ui_family, 10))
            style.configure("TNotebook.Tab", padding=(14, 6))
            style.configure("Accent.TButton", padding=(14, 6))

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)

        header = ttk.Label(
            container,
            text="MathTranslate v2 — Dịch bài báo & tài liệu LaTeX bảo toàn công thức toán",
            font=("Segoe UI", 12, "bold") if sys.platform == "win32" else ("TkDefaultFont", 12, "bold"),
        )
        header.grid(row=0, column=0, sticky="w", pady=(0, 8))

        paned = ttk.PanedWindow(container, orient=tk.VERTICAL)
        paned.grid(row=1, column=0, sticky="nsew")

        self.notebook = ttk.Notebook(paned)
        paned.add(self.notebook, weight=0)

        self._build_arxiv_tab()
        self._build_tex_tab()
        self._build_zip_tab()
        self._build_pdf_tab()
        self._build_settings_tab()

        log_frame = self._build_log_panel(paned)
        paned.add(log_frame, weight=1)

        status_bar = ttk.Frame(container, padding=(0, 8, 0, 0))
        status_bar.grid(row=2, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)

        ttk.Label(status_bar, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=1, sticky="e")

    def _add_path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label_text: str,
        var: tk.StringVar,
        browse_cmd,
        browse_label: str = "Chọn...",
        extra_cmd=None,
        extra_label: str = "",
    ) -> ttk.Entry:
        ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=6)
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", pady=6)
        btn_frame = ttk.Frame(parent)
        btn_frame.grid(row=row, column=2, sticky="e", padx=(8, 0), pady=6)
        ttk.Button(btn_frame, text=browse_label, command=browse_cmd).pack(side=tk.LEFT)
        if extra_cmd is not None and extra_label:
            ttk.Button(btn_frame, text=extra_label, command=extra_cmd).pack(side=tk.LEFT, padx=(6, 0))
        return entry

    def _build_arxiv_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        tab.columnconfigure(1, weight=1)
        self.notebook.add(tab, text="ArXiv")

        ttk.Label(tab, text="ArXiv ID hoặc URL:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(tab, textvariable=self.arxiv_id_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)

        self._add_path_row(
            tab,
            row=1,
            label_text="File .zip đầu ra:",
            var=self.arxiv_out_var,
            browse_cmd=self._browse_arxiv_output,
            browse_label="Lưu thành...",
        )

        ttk.Checkbutton(
            tab,
            text="Tự động biên dịch PDF (XeLaTeX)",
            variable=self.compile_pdf_var,
        ).grid(row=2, column=1, sticky="w", pady=(6, 10))

        btn = ttk.Button(
            tab,
            text="Dịch ArXiv",
            style="Accent.TButton",
            command=self._on_translate_arxiv,
        )
        btn.grid(row=3, column=1, sticky="w", pady=(2, 4))
        self._translate_buttons.append(btn)

    def _build_tex_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        tab.columnconfigure(1, weight=1)
        self.notebook.add(tab, text="File .tex")

        self._add_path_row(
            tab,
            row=0,
            label_text="File .tex đầu vào:",
            var=self.tex_in_var,
            browse_cmd=self._browse_tex_input,
            browse_label="Chọn file...",
        )
        self._add_path_row(
            tab,
            row=1,
            label_text="File .tex đầu ra:",
            var=self.tex_out_var,
            browse_cmd=self._browse_tex_output,
            browse_label="Lưu thành...",
        )

        ttk.Checkbutton(
            tab,
            text="Tự động biên dịch PDF (XeLaTeX)",
            variable=self.compile_pdf_var,
        ).grid(row=2, column=1, sticky="w", pady=(6, 10))

        btn = ttk.Button(
            tab,
            text="Dịch File .tex",
            style="Accent.TButton",
            command=self._on_translate_tex,
        )
        btn.grid(row=3, column=1, sticky="w", pady=(2, 4))
        self._translate_buttons.append(btn)

    def _build_zip_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        tab.columnconfigure(1, weight=1)
        self.notebook.add(tab, text="Project .zip")

        self._add_path_row(
            tab,
            row=0,
            label_text="Project .zip đầu vào:",
            var=self.zip_in_var,
            browse_cmd=self._browse_zip_input,
            browse_label="Chọn file...",
        )
        self._add_path_row(
            tab,
            row=1,
            label_text="Project .zip đầu ra:",
            var=self.zip_out_var,
            browse_cmd=self._browse_zip_output,
            browse_label="Lưu thành...",
        )

        ttk.Checkbutton(
            tab,
            text="Tự động biên dịch PDF (XeLaTeX)",
            variable=self.compile_pdf_var,
        ).grid(row=2, column=1, sticky="w", pady=(6, 10))

        btn = ttk.Button(
            tab,
            text="Dịch Project .zip",
            style="Accent.TButton",
            command=self._on_translate_zip,
        )
        btn.grid(row=3, column=1, sticky="w", pady=(2, 4))
        self._translate_buttons.append(btn)

    def _build_pdf_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        tab.columnconfigure(1, weight=1)
        self.notebook.add(tab, text="PDF Document (EN → VI)")

        self._add_path_row(
            tab,
            row=0,
            label_text="File .pdf đầu vào:",
            var=self.pdf_in_var,
            browse_cmd=self._browse_pdf_input,
            browse_label="Chọn PDF...",
            extra_cmd=self._on_inspect_pdf,
            extra_label="Kiểm tra",
        )
        self._add_path_row(
            tab,
            row=1,
            label_text="File .pdf đầu ra:",
            var=self.pdf_out_var,
            browse_cmd=self._browse_pdf_output,
            browse_label="Lưu thành...",
        )

        ttk.Label(tab, text="Trang scan (không tự OCR):").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=6)
        scan_combo = ttk.Combobox(
            tab,
            textvariable=self.pdf_scan_policy_var,
            values=["abort", "keep_original", "skip"],
            state="readonly",
            width=18,
        )
        scan_combo.grid(row=2, column=1, sticky="w", pady=6)

        ttk.Label(tab, text="Glossary (JSON):").grid(row=3, column=0, sticky="nw", padx=(0, 8), pady=6)
        self.pdf_glossary_text = tk.Text(tab, height=3, width=48, font=self._mono_font)
        self.pdf_glossary_text.grid(row=3, column=1, columnspan=2, sticky="ew", pady=6)

        info_row = ttk.Frame(tab)
        info_row.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(2, 6))
        ttk.Label(info_row, textvariable=self.pdf_progress_var).pack(side=tk.LEFT)
        ttk.Label(info_row, textvariable=self.pdf_eta_var).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Label(info_row, textvariable=self.pdf_overflow_var).pack(side=tk.LEFT, padx=(16, 0))

        btn_row = ttk.Frame(tab)
        btn_row.grid(row=5, column=1, sticky="w", pady=(4, 4))
        btn_start = ttk.Button(
            btn_row,
            text="Dịch PDF",
            style="Accent.TButton",
            command=self._on_translate_pdf,
        )
        btn_start.pack(side=tk.LEFT)
        self._translate_buttons.append(btn_start)
        ttk.Button(btn_row, text="Hủy (Cancel)", command=self._on_cancel_pdf).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btn_row, text="Tiếp tục (Resume)", command=self._on_resume_pdf).pack(side=tk.LEFT, padx=(8, 0))

    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        tab.columnconfigure(1, weight=1)
        self.notebook.add(tab, text="Cài đặt")

        ttk.Label(tab, text="Engine dịch:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        self.engine_combo = ttk.Combobox(
            tab,
            textvariable=self.engine_var,
            values=list(AVAILABLE_ENGINES),
            state="readonly",
        )
        self.engine_combo.grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(tab, text="Ngôn ngữ nguồn:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        self.from_lang_combo = ttk.Combobox(
            tab,
            textvariable=self.from_lang_var,
            values=self._language_labels,
            state="readonly",
        )
        self.from_lang_combo.grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(tab, text="Ngôn ngữ đích:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=6)
        self.to_lang_combo = ttk.Combobox(
            tab,
            textvariable=self.to_lang_var,
            values=self._language_labels,
            state="readonly",
        )
        self.to_lang_combo.grid(row=2, column=1, sticky="ew", pady=6)

        ttk.Label(tab, text="Số luồng (Threads):").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=6)
        threads_row = ttk.Frame(tab)
        threads_row.grid(row=3, column=1, sticky="w", pady=6)
        self.threads_spin = ttk.Spinbox(
            threads_row,
            from_=0,
            to=32,
            textvariable=self.threads_var,
            width=8,
        )
        self.threads_spin.pack(side=tk.LEFT)
        ttk.Label(threads_row, text="(0 = Tự động)").pack(side=tk.LEFT, padx=(8, 0))

        self._add_path_row(
            tab,
            row=4,
            label_text="File -commands (tùy chọn):",
            var=self.commands_file_var,
            browse_cmd=self._browse_commands_file,
            browse_label="Chọn file...",
            extra_cmd=lambda: self.commands_file_var.set(""),
            extra_label="Xóa",
        )

        btn_row = ttk.Frame(tab)
        btn_row.grid(row=5, column=1, sticky="w", pady=(10, 4))
        ttk.Button(
            btn_row,
            text="Lưu cài đặt",
            style="Accent.TButton",
            command=self._on_save_settings,
        ).pack(side=tk.LEFT)
        ttk.Label(
            btn_row,
            text="API key (Gemini/OpenAI/DeepL/Tencent) đọc từ biến môi trường hoặc cấu hình CLI.",
        ).pack(side=tk.LEFT, padx=(12, 0))

    def _build_log_panel(self, parent: ttk.PanedWindow) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(0, 8, 0, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        top_row = ttk.Frame(frame)
        top_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        top_row.columnconfigure(0, weight=1)

        ttk.Label(top_row, text="Nhật ký tiến trình (Log):").grid(row=0, column=0, sticky="w")
        ttk.Button(top_row, text="Xóa log", command=self._clear_log).grid(row=0, column=1, sticky="e")

        self.log_text = scrolledtext.ScrolledText(
            frame,
            height=12,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=self._mono_font,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        return frame

    # --- File dialog helpers ---

    def _browse_arxiv_output(self) -> None:
        arxiv_id = normalize_arxiv_id(self.arxiv_id_var.get()) or "arxiv"
        default_name = f"{arxiv_id.replace('/', '-')}.zip"
        path = filedialog.asksaveasfilename(
            title="Chọn nơi lưu file .zip kết quả",
            defaultextension=".zip",
            initialfile=default_name,
            filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")],
        )
        if path:
            self.arxiv_out_var.set(path)

    def _browse_tex_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Chọn file .tex cần dịch",
            filetypes=[("LaTeX files", "*.tex"), ("All files", "*.*")],
        )
        if path:
            self.tex_in_var.set(path)
            if not self.tex_out_var.get().strip():
                stem, _ = os.path.splitext(path)
                self.tex_out_var.set(f"{stem}_translated.tex")

    def _browse_tex_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Chọn nơi lưu file .tex đã dịch",
            defaultextension=".tex",
            filetypes=[("LaTeX files", "*.tex"), ("All files", "*.*")],
        )
        if path:
            self.tex_out_var.set(path)

    def _browse_zip_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Chọn project .zip cần dịch",
            filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")],
        )
        if path:
            self.zip_in_var.set(path)
            if not self.zip_out_var.get().strip():
                stem, _ = os.path.splitext(path)
                self.zip_out_var.set(f"{stem}_translated.zip")

    def _browse_zip_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Chọn nơi lưu project .zip đã dịch",
            defaultextension=".zip",
            filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")],
        )
        if path:
            self.zip_out_var.set(path)

    def _browse_commands_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Chọn file -commands tùy chỉnh",
            filetypes=[("Text / JSON / Python files", "*.txt *.json *.py"), ("All files", "*.*")],
        )
        if path:
            self.commands_file_var.set(path)

    def _browse_pdf_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Chọn file .pdf cần dịch",
            filetypes=[("PDF documents", "*.pdf"), ("All files", "*.*")],
        )
        if path:
            self.pdf_in_var.set(path)
            if not self.pdf_out_var.get().strip():
                stem, _ = os.path.splitext(path)
                self.pdf_out_var.set(f"{stem}_translated.pdf")

    def _browse_pdf_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Chọn nơi lưu file .pdf đã dịch",
            defaultextension=".pdf",
            filetypes=[("PDF documents", "*.pdf"), ("All files", "*.*")],
        )
        if path:
            self.pdf_out_var.set(path)

    def _on_inspect_pdf(self) -> None:
        in_path = self.pdf_in_var.get().strip()
        if not in_path or not os.path.isfile(in_path):
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn file .pdf đầu vào hợp lệ.")
            return
        try:
            info = self.pdf_controller.inspect(in_path)
        except Exception as exc:
            messagebox.showerror("Lỗi kiểm tra PDF", str(exc))
            return
        counts = info.get("counts", {})
        scan_pages = info.get("scan_pages", [])
        self._append_log(
            f"[PDF Inspect] {info.get('total_pages', 0)} trang "
            f"(TEXT={counts.get('TEXT', 0)}, SCAN={counts.get('SCAN', 0)}, MIXED={counts.get('MIXED', 0)}), "
            f"scan_pages={scan_pages}"
        )
        default_glossary = info.get("default_glossary") or {}
        if default_glossary and not self.pdf_glossary_text.get("1.0", tk.END).strip():
            self.pdf_glossary_text.insert("1.0", json.dumps(default_glossary, ensure_ascii=False, indent=2))
        if scan_pages:
            messagebox.showwarning(
                "Phát hiện trang scan",
                f"Các trang scan (không tự OCR): {scan_pages}. Chọn scan_policy=keep_original hoặc skip để tiếp tục.",
            )

    def _on_translate_pdf(self) -> None:
        in_path = self.pdf_in_var.get().strip()
        out_path = self.pdf_out_var.get().strip()
        if not in_path or not os.path.isfile(in_path):
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn file .pdf đầu vào hợp lệ.")
            return
        if not out_path:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn đường dẫn file .pdf đầu ra.")
            return
        if self.pdf_controller.is_running or self.runner.is_running:
            messagebox.showinfo("Đang chạy", "Một tác vụ dịch khác đang chạy.")
            return
        glossary = None
        raw_glossary = self.pdf_glossary_text.get("1.0", tk.END).strip()
        if raw_glossary:
            try:
                parsed = json.loads(raw_glossary)
                if isinstance(parsed, dict):
                    glossary = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError as exc:
                messagebox.showerror("Lỗi Glossary JSON", str(exc))
                return
        prefs = self._collect_current_preferences()
        self._set_running_state(True, "Đang dịch PDF...")
        self.pdf_controller.start(
            in_path,
            out_path,
            engine=prefs.engine,
            l_from=prefs.l_from,
            l_to=prefs.l_to,
            scan_policy=self.pdf_scan_policy_var.get().strip() or "abort",
            glossary=glossary,
            threads=prefs.threads,
        )

    def _on_cancel_pdf(self) -> None:
        self.pdf_controller.cancel()
        self._append_log("[PDF] Đã gửi yêu cầu hủy.")

    def _on_resume_pdf(self) -> None:
        if self.pdf_controller.is_running:
            messagebox.showinfo("Đang chạy", "Tác vụ PDF vẫn đang chạy.")
            return
        try:
            self._set_running_state(True, "Đang tiếp tục dịch PDF...")
            self.pdf_controller.resume()
        except Exception as exc:
            self._set_running_state(False, f"Lỗi: {exc}")
            messagebox.showerror("Không thể tiếp tục", str(exc))

    # --- Settings & Translation Actions ---

    def _collect_current_preferences(self) -> GuiPreferences:
        engine = (self.engine_var.get() or "google").strip().lower()
        l_from = self._label_to_code.get(self.from_lang_var.get(), "en")
        l_to = self._label_to_code.get(self.to_lang_var.get(), "vi")
        try:
            threads = max(0, int((self.threads_var.get() or "0").strip()))
        except ValueError as exc:
            raise ValueError("Số luồng (Threads) phải là số nguyên >= 0.") from exc

        return GuiPreferences(
            engine=engine,
            l_from=l_from,
            l_to=l_to,
            threads=threads,
            compile_pdf=bool(self.compile_pdf_var.get()),
            commands_file=self.commands_file_var.get().strip(),
        )

    def _on_save_settings(self) -> None:
        try:
            prefs = self._collect_current_preferences()
            prefs.save()
        except Exception as exc:
            messagebox.showerror("Lỗi cài đặt", str(exc))
            return
        self.prefs = prefs
        self.threads_var.set(str(prefs.threads))
        self.status_var.set("Đã lưu cài đặt.")
        self._append_log(
            f"[Cài đặt] Đã lưu: engine={prefs.engine}, {prefs.l_from} -> {prefs.l_to}, "
            f"threads={prefs.threads}, compile_pdf={prefs.compile_pdf}"
        )

    def _on_translate_arxiv(self) -> None:
        arxiv_id = normalize_arxiv_id(self.arxiv_id_var.get())
        out_path = self.arxiv_out_var.get().strip()
        if not arxiv_id:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng nhập ArXiv ID hoặc URL.")
            return
        if not out_path:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn đường dẫn file .zip đầu ra.")
            return
        self._start_translation("arxiv", arxiv_id, out_path)

    def _on_translate_tex(self) -> None:
        in_path = self.tex_in_var.get().strip()
        out_path = self.tex_out_var.get().strip()
        if not in_path or not os.path.isfile(in_path):
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn file .tex đầu vào hợp lệ.")
            return
        if not out_path:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn đường dẫn file .tex đầu ra.")
            return
        self._start_translation("tex", in_path, out_path)

    def _on_translate_zip(self) -> None:
        in_path = self.zip_in_var.get().strip()
        out_path = self.zip_out_var.get().strip()
        if not in_path or not os.path.isfile(in_path):
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn file .zip đầu vào hợp lệ.")
            return
        if not out_path:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng chọn đường dẫn file .zip đầu ra.")
            return
        self._start_translation("zip", in_path, out_path)

    def _start_translation(self, mode: str, source: str, output_path: str) -> None:
        if self.runner.is_running:
            messagebox.showinfo("Đang chạy", "Một tác vụ dịch khác đang chạy. Vui lòng đợi hoàn tất.")
            return
        try:
            prefs = self._collect_current_preferences()
            self._set_running_state(True, f"Đang dịch ({mode})...")
            self._append_log(f"--- Bắt đầu [{mode}]: {source} -> {output_path} ---")
            self.runner.start(mode, source, output_path, prefs=prefs)
        except Exception as exc:
            self._set_running_state(False, f"Lỗi: {exc}")
            messagebox.showerror("Không thể bắt đầu", str(exc))

    def _set_running_state(self, running: bool, status_text: str) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        for btn in self._translate_buttons:
            btn.configure(state=state)
        self.status_var.set(status_text)
        if running:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_queues(self) -> None:
        lines: list[str] = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, "\n".join(lines) + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        while True:
            try:
                ok, message = self.status_queue.get_nowait()
            except queue.Empty:
                break
            status_prefix = "Hoàn tất" if ok else "Thất bại"
            self._set_running_state(False, f"{status_prefix}: {message}")
            self._append_log(f"[{status_prefix}] {message}")

        while True:
            try:
                prog = self.pdf_controller.progress_queue.get_nowait()
            except queue.Empty:
                break
            self.pdf_progress_var.set(
                f"Tiến độ PDF: {prog.get('completed_pages', 0)} / {prog.get('total_pages', 0)} trang"
            )
            eta = prog.get("eta_seconds")
            self.pdf_eta_var.set(f"ETA: {eta:.1f}s" if isinstance(eta, (int, float)) else "ETA: --")
            overflow = prog.get("overflow_pages") or []
            self.pdf_overflow_var.set(
                f"Trang tràn chữ: {', '.join(map(str, overflow))}" if overflow else "Trang tràn chữ: Không có"
            )

        self._poll_after_id = self.after(self.POLL_INTERVAL_MS, self._poll_queues)

    def destroy(self) -> None:
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        super().destroy()


def run_gui() -> None:
    """Launch the MathTranslate Desktop GUI application."""
    _enable_windows_dpi_awareness()
    app = MathTranslateApp()
    app.mainloop()


if __name__ == "__main__":
    run_gui()
