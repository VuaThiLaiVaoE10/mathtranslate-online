"""Offline deterministic E2E test harness for PDF-M2 and PDF-M3."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable
import unittest.mock as mock

import pymupdf
import pytest
import requests

V2_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = V2_ROOT.parent
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

REF_PAPER_PDF = REPO_ROOT / "reference" / "example" / "paper.pdf"
LINEAR_PDF = REPO_ROOT / "pdf2zh_files" / "linear.pdf"
WEB_DIR = V2_ROOT / "web"


def make_http_error(status_code: int = 429, message: str = "Rate limit exceeded") -> requests.HTTPError:
    """Create a requests.HTTPError carrying an HTTP status code compatible with BaseTranslator."""
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = message.encode("utf-8")
    return requests.HTTPError(f"HTTP {status_code}: {message}", response=resp)


class DeterministicE2ETranslator:
    """Offline deterministic translator supporting NMT batching, LLM context prompts,
    concurrency tracking, retryable failure injection, and mid-run cancel hooks.
    """

    _WORD_MAP: tuple[tuple[str, str], ...] = (
        ("linear transformation", "ánh xạ tuyến tính"),
        ("vector space", "không gian vectơ"),
        ("boundary condition", "điều kiện biên"),
        ("eigenvalues", "các giá trị riêng"),
        ("eigenvalue", "giá trị riêng"),
        ("eigenvector", "vectơ riêng"),
        ("determinant", "định thức"),
        ("matrices", "các ma trận"),
        ("matrix", "ma trận"),
        ("theorem", "định lý"),
        ("lemma", "bổ đề"),
        ("corollary", "hệ quả"),
        ("proposition", "mệnh đề"),
        ("equation", "phương trình"),
        ("equations", "các phương trình"),
        ("proof", "chứng minh"),
        ("section", "mục"),
        ("chapter", "chương"),
        ("function", "hàm số"),
        ("subspace", "không gian con"),
        ("space", "không gian"),
        ("basis", "cơ sở"),
        ("linear", "tuyến tính"),
        ("every", "mọi"),
        ("has", "có"),
        ("this", "này"),
        ("the", "các"),
        ("and", "và"),
        ("is", "là"),
        ("in", "trong"),
        ("of", "của"),
        ("for", "cho"),
        ("with", "với"),
    )
    _VI_DIACRITIC_RE = re.compile(
        r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
        r"ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ]"
    )

    def __init__(
        self,
        engine: str = "mock",
        language_from: str = "en",
        language_to: str = "vi",
        *,
        fail_on_attempts: dict[int, BaseException] | None = None,
        fail_first_n: int = 0,
        always_fail: BaseException | None = None,
        per_call_delay: float = 0.0,
        on_call_hook: Callable[[int, str], None] | None = None,
    ) -> None:
        self.engine = engine
        self.language_from = language_from
        self.language_to = language_to
        self.fail_on_attempts = dict(fail_on_attempts or {})
        self.fail_first_n = fail_first_n
        self.always_fail = always_fail
        self.per_call_delay = per_call_delay
        self.on_call_hook = on_call_hook

        self._lock = threading.Lock()
        self.calls: list[str] = []
        self.attempts: list[str] = []
        self.call_count: int = 0
        self.attempt_count: int = 0
        self.thread_ids: list[int] = []
        self.active_calls: int = 0
        self.max_active_calls: int = 0

    def reset_tracking(self) -> None:
        with self._lock:
            self.calls.clear()
            self.attempts.clear()
            self.call_count = 0
            self.attempt_count = 0
            self.thread_ids.clear()
            self.active_calls = 0
            self.max_active_calls = 0

    def _translate_single_fragment(self, fragment: str) -> str:
        assert "\uf0b1" not in fragment, "MathType PUA symbol leaked into translator!"
        # Preserve XMATHX_\d+ tokens verbatim without altering them
        parts = re.split(r"(XMATHX_\d+)", fragment)
        out_parts: list[str] = []
        for part in parts:
            if re.fullmatch(r"XMATHX_\d+", part):
                out_parts.append(part)
                continue
            transformed = part
            for src, dst in self._WORD_MAP:
                transformed = re.sub(rf"\b{re.escape(src)}\b", dst, transformed, flags=re.IGNORECASE)
            out_parts.append(transformed)
        result = "".join(out_parts)
        non_ph = re.sub(r"XMATHX(?:_\d+)?", "", fragment)
        if re.search(r"[A-Za-z]{2,}", non_ph) and not self._VI_DIACRITIC_RE.search(result):
            result = f"Bản dịch tiếng Việt: {result}"
        return result

    def _transform_text(self, text: str) -> str:
        marker = "[Translate ONLY this paragraph]:"
        body = text
        if marker in body:
            _, target_body = body.split(marker, 1)
            body = target_body.lstrip("\r\n")

        if "XMATHXSEP" in body:
            pieces = re.split(r"(\s*XMATHXSEP\s*)", body)
            out = []
            for p in pieces:
                if re.fullmatch(r"\s*XMATHXSEP\s*", p):
                    out.append(p)
                else:
                    out.append(self._translate_single_fragment(p))
            return "".join(out)

        return self._translate_single_fragment(body)

    def translate(self, text: str) -> str:
        with self._lock:
            self.attempt_count += 1
            attempt_idx = self.attempt_count
            self.attempts.append(text)
            self.thread_ids.append(threading.get_ident())
            self.active_calls += 1
            if self.active_calls > self.max_active_calls:
                self.max_active_calls = self.active_calls

        try:
            if self.per_call_delay > 0:
                time.sleep(self.per_call_delay)

            if self.always_fail is not None:
                raise self.always_fail
            if attempt_idx <= self.fail_first_n:
                raise make_http_error(429, f"Simulated 429 on attempt {attempt_idx}")
            if attempt_idx in self.fail_on_attempts:
                raise self.fail_on_attempts[attempt_idx]

            translated = self._transform_text(text)

            with self._lock:
                self.call_count += 1
                call_idx = self.call_count
                self.calls.append(text)

            if self.on_call_hook is not None:
                self.on_call_hook(call_idx, text)
            return translated
        finally:
            with self._lock:
                self.active_calls -= 1


def _make_raster_pixmap(width: int = 400, height: int = 600, fill: tuple[float, float, float] = (0.85, 0.85, 0.85)) -> pymupdf.Pixmap:
    with pymupdf.open() as img_doc:
        ip = img_doc.new_page(width=width, height=height)
        ip.draw_rect(pymupdf.Rect(0, 0, width, height), color=(0.2, 0.2, 0.2), fill=fill)
        return ip.get_pixmap(dpi=24)


@dataclass
class PdfFactory:
    """Fast synthetic PyMuPDF builders with strict context-managed cleanup."""

    def make_text_pdf(
        self,
        path: Path,
        num_pages: int = 3,
        *,
        dense: bool = False,
        with_toc: bool = False,
        with_formulas: bool = True,
        with_ambiguous_terms: bool = True,
        with_image: bool = False,
        tight_box: bool = False,
        extra_text: str = "",
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fig_pix = _make_raster_pixmap(80, 50, fill=(0.2, 0.4, 0.7)) if with_image else None
        toc_entries: list[list[Any]] = []
        with pymupdf.open() as doc:
            for pno in range(num_pages):
                page = doc.new_page(width=612, height=792)
                if tight_box:
                    written = page.insert_textbox(
                        pymupdf.Rect(50, 50, 230, 76),
                        "Short English sentence in box.",
                        fontsize=10,
                    )
                    assert written >= 0, f"tight_box source text failed to fit: {written}"
                    continue

                y = 72
                header = f"Section {pno + 1}: Eigenvalue Theorem and Vector Space Analysis"
                page.insert_text((72, y), header, fontsize=12)
                y += 26

                base_para = (
                    f"On page {pno + 1}, every matrix has an eigenvalue in this linear vector space "
                    "and the boundary condition determines the proof structure cleanly."
                )
                if with_ambiguous_terms:
                    base_para += (
                        " The algebraic field contains the kernel of the linear transformation "
                        "and its matrix rank over the base field."
                    )
                if extra_text:
                    base_para += f" {extra_text}"

                if dense:
                    for para_idx in range(8):
                        rect = pymupdf.Rect(72, y, 540, y + 66)
                        page.insert_textbox(
                            rect,
                            f"Paragraph {para_idx + 1} (page {pno + 1}): {base_para} "
                            "Detailed mathematical exposition continues with multiple clauses "
                            "to ensure high character density per page for adaptive batching.",
                            fontsize=10,
                        )
                        y += 74
                else:
                    page.insert_textbox(pymupdf.Rect(72, y, 540, y + 70), base_para, fontsize=11)
                    y += 80

                if with_formulas:
                    page.insert_text(
                        (72, y),
                        "Inline formula with PUA symbol: \uf0b1 and citation [12] in the proof.",
                        fontsize=11,
                    )
                    y += 28
                    page.insert_text((220, y), "\u222b f(x) dx = \u2211 \u03b1_i \u00d7 \u03b2_i", fontsize=12)
                    page.draw_line((218, y + 5), (350, y + 5), color=(0, 0, 0), width=0.8)
                    y += 32

                if with_image and pno == 0 and fig_pix is not None:
                    page.insert_image(pymupdf.Rect(200, min(y, 620), 360, min(y + 80, 700)), pixmap=fig_pix)

                if with_toc:
                    toc_entries.append([1, f"Chapter {pno + 1}", pno + 1])

            if with_toc and toc_entries:
                doc.set_toc(toc_entries)
            doc.set_metadata({"title": "E2E Synthetic Math Document", "author": "MathTranslate E2E"})
            doc.save(str(path))
        return path

    def make_scan_pdf(self, path: Path, num_pages: int = 2) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        pix = _make_raster_pixmap(400, 600)
        with pymupdf.open() as doc:
            for _ in range(num_pages):
                page = doc.new_page(width=595, height=842)
                page.insert_image(pymupdf.Rect(40, 40, 555, 790), pixmap=pix)
            doc.save(str(path))
        return path

    def make_mixed_pdf(self, path: Path, num_pages: int = 1) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        pix = _make_raster_pixmap(400, 450)
        with pymupdf.open() as doc:
            for pno in range(num_pages):
                page = doc.new_page(width=600, height=800)
                page.insert_image(pymupdf.Rect(50, 260, 550, 740), pixmap=pix)
                page.insert_textbox(
                    pymupdf.Rect(60, 50, 540, 180),
                    f"Page {pno + 1}: This mixed page contains clear selectable English prose "
                    "above a large embedded figure covering more than forty percent of page area.",
                    fontsize=11,
                )
            doc.save(str(path))
        return path

    def make_scan_and_text_pdf(
        self,
        path: Path,
        total_pages: int = 4,
        scan_pages: tuple[int, ...] = (1,),
        *,
        with_ambiguous_terms: bool = True,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        scan_set = set(scan_pages)
        pix = _make_raster_pixmap(400, 600)
        with pymupdf.open() as doc:
            for pno in range(total_pages):
                if pno in scan_set:
                    page = doc.new_page(width=595, height=842)
                    page.insert_image(pymupdf.Rect(40, 40, 555, 790), pixmap=pix)
                else:
                    page = doc.new_page(width=612, height=792)
                    txt = (
                        f"Page {pno + 1}: Every matrix has an eigenvalue in this vector space "
                        "and the theorem follows directly from the proof."
                    )
                    if with_ambiguous_terms:
                        txt += " The field and kernel and rank of the linear map over the field are invariant."
                    page.insert_textbox(pymupdf.Rect(72, 72, 540, 200), txt, fontsize=11)
            doc.save(str(path))
        return path

    def make_empty_pdf(self, path: Path, num_pages: int = 1) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open() as doc:
            for _ in range(num_pages):
                doc.new_page(width=612, height=792)
            doc.save(str(path))
        return path


@pytest.fixture()
def pdf_factory() -> PdfFactory:
    return PdfFactory()


@pytest.fixture()
def make_translator() -> Callable[..., DeterministicE2ETranslator]:
    def _factory(**kwargs: Any) -> DeterministicE2ETranslator:
        return DeterministicE2ETranslator(**kwargs)
    return _factory


@pytest.fixture()
def patch_offline_text_translator(monkeypatch):
    """Monkeypatch mathtranslate.translate.TextTranslator to delegate offline to DeterministicE2ETranslator."""
    def _apply(translator: DeterministicE2ETranslator | None = None, **kwargs: Any) -> DeterministicE2ETranslator:
        from mathtranslate import translate as translate_mod
        from mathtranslate.pdf_batch import _FALLBACK_CLASSIFIER

        delegate = translator or DeterministicE2ETranslator(**kwargs)

        class _OfflineEngineAdapter:
            def __init__(self, target: DeterministicE2ETranslator, eng: str) -> None:
                self._target = target
                self.name = str(eng)
                self.supports_multithread = getattr(
                    target, "supports_multithread", str(eng).strip().lower() != "tencent"
                )

            def translate(self, text: str, language_to: str, language_from: str) -> str:
                return self._target.translate(text)

            def is_retryable_error(self, exc: BaseException) -> bool:
                fn = getattr(self._target, "is_retryable_error", None)
                if callable(fn):
                    return bool(fn(exc))
                return _FALLBACK_CLASSIFIER.is_retryable_error(exc)

            def is_error_request_frequency(self, exc: BaseException) -> bool:
                fn = getattr(self._target, "is_error_request_frequency", None)
                if callable(fn):
                    return bool(fn(exc))
                return _FALLBACK_CLASSIFIER.is_error_request_frequency(exc)

        def fake_init(self, engine, language_to, language_from):
            self.engine = engine
            self.language_to = language_to
            self.language_from = language_from
            self.number_of_calls = 0
            self.tot_char = 0
            self._e2e_delegate = delegate
            self._e2e_lock = threading.Lock()
            self.translator = _OfflineEngineAdapter(delegate, engine)
            self.try_translate = lambda text: self.translator.translate(
                text, self.language_to, self.language_from
            )

        def fake_translate(self, text):
            if not re.match(r".*[a-zA-Z].*", text, re.DOTALL):
                return text
            result = self.try_translate(text)
            with self._e2e_lock:
                self.number_of_calls += 1
                self.tot_char += len(text)
            return result

        monkeypatch.setattr(translate_mod.TextTranslator, "__init__", fake_init)
        monkeypatch.setattr(translate_mod.TextTranslator, "translate", fake_translate)
        return delegate

    return _apply


def make_cancel_barrier(
    page_pattern: str,
    *,
    timeout: float = 15.0,
) -> tuple[Callable[[int, str], None], threading.Event, threading.Event]:
    """Create a deterministic on_call_hook barrier that blocks once `page_pattern` matches."""
    pattern = re.compile(page_pattern)
    entered_gate = threading.Event()
    release_gate = threading.Event()

    def _hook(call_idx: int, text: str) -> None:
        if pattern.search(text):
            entered_gate.set()
            release_gate.wait(timeout=timeout)

    return _hook, entered_gate, release_gate


def post_pdf(
    client,
    url: str,
    pdf_path: Path | None = None,
    *,
    filename: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """POST multipart/form-data cleanly using BytesIO and closing TestResponse to prevent ResourceWarning."""
    import io

    data: dict[str, Any] = dict(extra_data or {})
    if pdf_path is not None:
        fname = filename or pdf_path.name
        data["file"] = (io.BytesIO(pdf_path.read_bytes()), fname)
    with client.post(url, data=data, content_type="multipart/form-data") as resp:
        return resp.status_code, resp.get_json()


def poll_web_job(
    client,
    job_id: str,
    *,
    stop_statuses: set[str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Poll GET /api/jobs/<job_id> until status reaches one of `stop_statuses`."""
    targets = stop_statuses or {"done", "error", "awaiting_scan", "cancelled"}
    deadline = time.time() + timeout
    last_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        with client.get(f"/api/jobs/{job_id}") as resp:
            assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.data}"
            last_payload = resp.get_json()
        if last_payload["status"] in targets:
            return last_payload
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not reach {targets} within {timeout}s; last={last_payload}")


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    """Isolated Flask test client with deterministic job cleanup before tmp_path teardown."""
    import web.app as web_app

    jobs_root = tmp_path / "web_jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setitem(web_app.app.config, "JOBS_ROOT", str(jobs_root))
    monkeypatch.setitem(web_app.app.config, "TESTING", True)

    with web_app.JOBS_LOCK:
        web_app.JOBS.clear()

    try:
        with web_app.app.test_client() as test_client:
            yield test_client
    finally:
        # Drain any active background threads/jobs so SQLite/PDF handles close before tmp_path cleanup
        deadline = time.time() + 10.0
        while time.time() < deadline:
            with web_app.JOBS_LOCK:
                active = [
                    rec for rec in web_app.JOBS.values()
                    if rec.get("status") in {"queued", "running"}
                ]
                for rec in active:
                    bt = rec.get("_batch_translator")
                    if bt is not None and hasattr(bt, "cancel"):
                        try:
                            bt.cancel()
                        except Exception:
                            pass
            if not active:
                break
            time.sleep(0.05)
        with web_app.JOBS_LOCK:
            web_app.JOBS.clear()


@pytest.fixture()
def isolated_gui_prefs(tmp_path, monkeypatch) -> Path:
    from mathtranslate import config as config_module

    temp_default_dir = tmp_path / "gui_default"
    temp_default_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_module, "default_dir", str(temp_default_dir))
    return temp_default_dir


@pytest.fixture()
def gui_app(isolated_gui_prefs, monkeypatch):
    """Headless-safe MathTranslateApp fixture with mocked dialogs and guaranteed cleanup."""
    import tkinter as tk
    import gui.app as gui_app_mod

    monkeypatch.setattr(gui_app_mod.filedialog, "askopenfilename", lambda **kw: "")
    monkeypatch.setattr(gui_app_mod.filedialog, "asksaveasfilename", lambda **kw: "")
    monkeypatch.setattr(gui_app_mod.messagebox, "showerror", lambda *a, **kw: None)
    monkeypatch.setattr(gui_app_mod.messagebox, "showinfo", lambda *a, **kw: None)
    monkeypatch.setattr(gui_app_mod.messagebox, "askyesnocancel", lambda *a, **kw: None)
    monkeypatch.setattr(gui_app_mod.messagebox, "askyesno", lambda *a, **kw: False)
    monkeypatch.setattr(gui_app_mod.messagebox, "askokcancel", lambda *a, **kw: False)

    try:
        app = gui_app_mod.MathTranslateApp()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")

    try:
        yield app
    finally:
        # Stop any active runner or PDF controller before destroying Tk root
        for attr in ("runner", "pdf_controller", "pdf_runner"):
            ctrl = getattr(app, attr, None)
            if ctrl is not None:
                if hasattr(ctrl, "cancel"):
                    try:
                        ctrl.cancel()
                    except Exception:
                        pass
                if hasattr(ctrl, "wait"):
                    try:
                        ctrl.wait(timeout=5)
                    except Exception:
                        pass
        app.destroy()
