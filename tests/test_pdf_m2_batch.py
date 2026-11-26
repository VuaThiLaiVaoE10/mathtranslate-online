"""Stage PDF-M2 automated tests: density batching, SQLite TM, retry/backoff, progressive partial.pdf, cancel/resume, and R2 quality."""

from __future__ import annotations

import concurrent.futures
import io
import json
from pathlib import Path
import re
import threading
import time
import urllib.error

import pymupdf
import pytest
import requests

from mathtranslate.pdf_batch import (
    BatchCancelledError,
    BatchCheckpoint,
    PdfBatchTranslator,
    SQLiteTranslationMemory,
    _RetryingTranslatorProxy,
    compute_density_batches,
    compute_glossary_hash,
    inspect_pdf,
)
from mathtranslate.pdf_doc import (
    DEFAULT_EN_VI_GLOSSARY,
    ScanPagesDetectedError,
    _LLM_PROMPT_HEADER_LINE_RE,
    _plan_page_blocks,
    _strip_llm_prompt_leaks,
    classify_pdf_pages,
    extract_ambiguous_terms,
    translate_pdf_file,
)
from mathtranslate.pdf_qa import verify_translated_pdf
from mathtranslate.translate import TextTranslator

REPO_ROOT = Path(__file__).resolve().parents[2]
LINEAR_PDF = REPO_ROOT / "pdf2zh_files" / "linear.pdf"


class OfflineViMockTranslator:
    """Thread-safe deterministic EN->VI mock translator with call tracking."""

    def __init__(self, engine: str = "mock", language_to: str = "vi", language_from: str = "en") -> None:
        self.engine = engine
        self.language_to = language_to
        self.language_from = language_from
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def translate(self, text: str) -> str:
        with self._lock:
            self.calls.append(text)
        assert "\uf0b1" not in text, "MathType PUA symbol leaked into translator!"
        out = text
        replacements = [
            ("Chapter", "Chương"),
            ("Theorem", "Định lý"),
            ("Section", "Mục"),
            ("equations", "các phương trình"),
            ("equation", "phương trình"),
            ("the", "các"),
            ("and", "và"),
            ("is", "là"),
            ("for", "cho"),
            ("with", "với"),
            ("in", "trong"),
        ]
        for k, v in replacements:
            out = re.sub(rf"\b{k}\b", v, out, flags=re.IGNORECASE)
        if out == text:
            out = f"Bản dịch tiếng Việt chuẩn: {out}"
        return out


def _build_multipage_math_pdf(path: Path, num_pages: int = 16) -> None:
    doc = pymupdf.open()
    fig_doc = pymupdf.open()
    fp = fig_doc.new_page(width=60, height=40)
    fp.draw_rect(pymupdf.Rect(0, 0, 60, 40), color=(0.1, 0.2, 0.5), fill=(0.2, 0.4, 0.7))
    pix = fp.get_pixmap(dpi=72)
    fig_doc.close()

    toc = []
    for pno in range(num_pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text(
            (72, 90),
            f"Section {pno + 1}: Every matrix has an eigenvalue in this vector space.",
            fontsize=11,
        )
        page.insert_text(
            (72, 130),
            f"Page {pno + 1} discusses theorem proof with inline symbol \uf0b1 and citation [{pno + 1}].",
            fontsize=11,
        )
        page.insert_text((220, 210), "\u222b f(x) dx = \u2211 \u03b1_i", fontsize=12)
        page.insert_image(pymupdf.Rect(200, 260, 280, 310), pixmap=pix)
        if pno % 4 == 0:
            toc.append([1, f"Chapter {pno // 4 + 1}", pno + 1])

    doc.set_toc(toc)
    doc.set_metadata({"title": "Synthetic Batch Math Book", "author": "M2 Test"})
    doc.save(str(path))
    doc.close()


def _build_scan_plus_text_pdf(path: Path) -> None:
    """Create a 3-page PDF: [TEXT, SCAN, TEXT] with TOC entries."""
    doc = pymupdf.open()
    p0 = doc.new_page(width=595, height=842)
    p0.insert_text((72, 100), "Chapter 1: Every matrix has an eigenvalue and determinant in this space.", fontsize=11)

    p1 = doc.new_page(width=595, height=842)
    img_doc = pymupdf.open()
    ip = img_doc.new_page(width=400, height=600)
    ip.draw_rect(pymupdf.Rect(0, 0, 400, 600), color=(0.2, 0.2, 0.2), fill=(0.85, 0.85, 0.85))
    scan_pix = ip.get_pixmap(dpi=72)
    img_doc.close()
    p1.insert_image(pymupdf.Rect(30, 30, 565, 810), pixmap=scan_pix)

    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((72, 100), "Chapter 2: The linear transformation preserves every vector space basis.", fontsize=11)

    doc.set_toc([[1, "Intro Text", 1], [1, "Scanned Appendix", 2], [1, "Chapter 2", 3]])
    doc.save(str(path))
    doc.close()


def test_compute_density_batches_and_linear_pdf():
    assert compute_density_batches([], {}) == []
    assert compute_density_batches([0, 1, 2], {0: 5000, 1: 5000, 2: 5000}) == [[0, 1, 2]]

    # Sparse pages group up to 10 pages
    sparse_counts = {i: 400 for i in range(20)}
    sparse_batches = compute_density_batches(list(range(20)), sparse_counts)
    assert sparse_batches == [list(range(0, 10)), list(range(10, 20))]

    # Dense pages split down to 5 pages
    dense_counts = {i: 3200 for i in range(15)}
    dense_batches = compute_density_batches(list(range(15)), dense_counts)
    assert dense_batches == [list(range(0, 5)), list(range(5, 10)), list(range(10, 15))]

    # Tail rebalancing: 12 dense pages -> never leaves a 2-page tail
    rebal_counts = {i: 3200 for i in range(12)}
    rebal_batches = compute_density_batches(list(range(12)), rebal_counts)
    assert [p for b in rebal_batches for p in b] == list(range(12))
    assert all(5 <= len(b) <= 10 for b in rebal_batches)

    # Real 386-page linear.pdf
    assert LINEAR_PDF.is_file()
    with pymupdf.open(str(LINEAR_PDF)) as doc:
        cls = classify_pdf_pages(doc)
    assert len(cls) == 386
    lin_batches = compute_density_batches(list(range(386)), cls)
    assert [p for b in lin_batches for p in b] == list(range(386))
    assert all(5 <= len(b) <= 10 for b in lin_batches)
    assert any(len(b) == 10 for b in lin_batches)  # sparse front-matter/TOC pages
    assert any(len(b) <= 7 for b in lin_batches)   # dense math chapter pages


def test_sqlite_tm_sha256_sensitivity_persistence_and_concurrency(tmp_path):
    h1 = compute_glossary_hash({"matrix": "ma trận"})
    h2 = compute_glossary_hash({"matrix": "ma trận", "Matrix": "ma trận chuẩn"})
    h3 = compute_glossary_hash({"Matrix": "ma trận chuẩn"})
    assert h1 != h2
    assert h2 == h3  # case-insensitive override collapses identically

    db_path = tmp_path / "pdf_tm.sqlite3"
    tm = SQLiteTranslationMemory(db_path)
    try:
        k_base = tm.make_key("Hello matrix", h1, "google", "en", "vi")
        assert k_base != tm.make_key("Hello matrix!", h1, "google", "en", "vi")
        assert k_base != tm.make_key("Hello matrix", h2, "google", "en", "vi")
        assert k_base != tm.make_key("Hello matrix", h1, "gemini", "en", "vi")
        assert k_base != tm.make_key("Hello matrix", h1, "google", "fr", "vi")
        assert k_base != tm.make_key("Hello matrix", h1, "google", "en", "ja")

        tm.store("Hello matrix", "Xin chào ma trận", h1, "google", "en", "vi")
        assert tm.lookup("Hello matrix", h1, "google", "en", "vi") == "Xin chào ma trận"
        assert tm.lookup("Hello matrix", h2, "google", "en", "vi") is None
        assert tm.count() == 1
    finally:
        tm.close()

    # Reopen persists entries & supports concurrent multi-thread access
    tm2 = SQLiteTranslationMemory(db_path)
    try:
        assert tm2.lookup("Hello matrix", h1, "google", "en", "vi") == "Xin chào ma trận"

        def _worker(idx: int) -> str | None:
            src = f"Segment {idx}"
            tgt = f"Đoạn {idx}"
            tm2.store(src, tgt, h1, "google", "en", "vi")
            return tm2.lookup(src, h1, "google", "en", "vi")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_worker, range(60)))
        assert results == [f"Đoạn {i}" for i in range(60)]
        assert tm2.count() == 61
    finally:
        tm2.close()


def test_retrying_proxy_backoff_fast_fail_and_worker_clamping(tmp_path):
    # 1. Exponential backoff on HTTP 429 & TimeoutError
    sleeps: list[float] = []
    cancel_ev = threading.Event()
    orig_wait = cancel_ev.wait

    def _recording_wait(timeout: float | None = None) -> bool:
        if timeout is not None:
            sleeps.append(round(timeout, 4))
        return orig_wait(0.0)

    cancel_ev.wait = _recording_wait  # type: ignore[method-assign]

    attempts = {"n": 0}

    class FlakyTranslator:
        engine = "mock"

        def translate(self, text: str) -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, io.BytesIO(b""))
            if attempts["n"] == 2:
                raise TimeoutError("timed out")
            return f"Dịch: {text}"

    proxy = _RetryingTranslatorProxy(
        FlakyTranslator(),
        engine="mock",
        l_from="en",
        l_to="vi",
        max_retries=4,
        base_backoff=0.25,
        cancel_event=cancel_ev,
    )
    assert proxy.translate("Hello world") == "Dịch: Hello world"
    assert attempts["n"] == 3
    assert sleeps == [0.25, 0.5]

    # 2. TextTranslator custom rate-limit exception does NOT infinite-loop (bypasses translate.py:60-61)
    freq_attempts = {"n": 0}
    tt = TextTranslator("google", "vi", "en")

    class AlwaysRateLimited:
        supports_multithread = True

        def is_error_request_frequency(self, exc: BaseException) -> bool:
            return "RequestLimitExceeded" in str(exc)

        def is_retryable_error(self, exc: BaseException) -> bool:
            return self.is_error_request_frequency(exc)

        def translate(self, text: str, l_to: str, l_from: str) -> str:
            freq_attempts["n"] += 1
            raise RuntimeError("RequestLimitExceeded")

    tt.translator = AlwaysRateLimited()
    cancel_ev2 = threading.Event()
    cancel_ev2.wait = lambda timeout=None: False  # type: ignore[method-assign]
    proxy2 = _RetryingTranslatorProxy(
        tt,
        engine="google",
        l_from="en",
        l_to="vi",
        max_retries=2,
        base_backoff=0.1,
        cancel_event=cancel_ev2,
    )
    with pytest.raises(RuntimeError, match="RequestLimitExceeded"):
        proxy2.translate("Hello")
    assert freq_attempts["n"] == 3  # 1 initial + 2 retries, never infinite

    # 3. Fast-fail on ValueError and HTTP 400/401/403/404 (0 retries)
    for err_factory in (
        lambda: ValueError("bad input"),
        lambda: urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, io.BytesIO(b"")),
        lambda: urllib.error.HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(b"")),
        lambda: requests.HTTPError(response=type("R", (), {"status_code": 403})()),
    ):
        bad_calls = {"n": 0}

        class BadTranslator:
            engine = "mock"

            def translate(self, text: str) -> str:
                bad_calls["n"] += 1
                raise err_factory()

        p_bad = _RetryingTranslatorProxy(
            BadTranslator(),
            engine="mock",
            l_from="en",
            l_to="vi",
            max_retries=4,
            base_backoff=1.0,
            cancel_event=threading.Event(),
        )
        with pytest.raises(Exception):
            p_bad.translate("Hello")
        assert bad_calls["n"] == 1

    # 4. cancel() wakes sleeping backoff thread immediately
    wake_ev = threading.Event()
    entered = threading.Event()

    class Sleepy429:
        engine = "mock"

        def translate(self, text: str) -> str:
            entered.set()
            raise TimeoutError("slow")

    p_wake = _RetryingTranslatorProxy(
        Sleepy429(),
        engine="mock",
        l_from="en",
        l_to="vi",
        max_retries=3,
        base_backoff=5.0,
        cancel_event=wake_ev,
    )
    t0 = time.monotonic()

    def _trigger_cancel() -> None:
        entered.wait(timeout=1.0)
        time.sleep(0.05)
        wake_ev.set()

    canceller = threading.Thread(target=_trigger_cancel)
    canceller.start()
    with pytest.raises(BatchCancelledError):
        p_wake.translate("Hello")
    canceller.join(timeout=1.0)
    assert time.monotonic() - t0 < 1.0

    # 5. supports_multithread=False clamps max_workers to 1
    dummy_pdf = tmp_path / "dummy.pdf"
    _build_multipage_math_pdf(dummy_pdf, num_pages=2)
    bt_tencent = PdfBatchTranslator(
        dummy_pdf,
        tmp_path / "out_tc.pdf",
        tmp_path / "work_tc",
        engine="tencent",
        max_workers=4,
    )
    assert bt_tencent._effective_workers() == 1

    class SingleThreadMock(OfflineViMockTranslator):
        supports_multithread = False

    bt_st = PdfBatchTranslator(
        dummy_pdf,
        tmp_path / "out_st.pdf",
        tmp_path / "work_st",
        max_workers=4,
        translator=SingleThreadMock(),
    )
    assert bt_st._effective_workers() == 1


def test_synthetic_multibatch_full_lifecycle_checkpoint_partial_and_qa(tmp_path):
    src_pdf = tmp_path / "book16.pdf"
    out_pdf = tmp_path / "book16_vi.pdf"
    work_dir = tmp_path / "work16"
    _build_multipage_math_pdf(src_pdf, num_pages=16)

    snapshots: list[BatchCheckpoint] = []
    active_lock = threading.Lock()
    active_workers = {"cur": 0, "peak": 0}

    class ConcurrencyTrackingMock(OfflineViMockTranslator):
        def translate(self, text: str) -> str:
            with active_lock:
                active_workers["cur"] += 1
                active_workers["peak"] = max(active_workers["peak"], active_workers["cur"])
            try:
                time.sleep(0.01)
                return super().translate(text)
            finally:
                with active_lock:
                    active_workers["cur"] -= 1

    def _on_prog(ckpt: BatchCheckpoint) -> None:
        snapshots.append(ckpt)
        # During run, partial.pdf must already be readable and checkpoint.json valid
        if ckpt.completed_batches:
            assert (work_dir / "checkpoint.json").is_file()
            with pymupdf.open(str(work_dir / "partial.pdf")) as pdoc:
                assert len(pdoc) == len(ckpt.completed_pages)

    bt = PdfBatchTranslator(
        src_pdf,
        out_pdf,
        work_dir,
        engine="mock",
        max_workers=2,
        min_batch_pages=5,
        max_batch_pages=6,
        translator_factory=ConcurrencyTrackingMock,
        on_progress=_on_prog,
    )
    final_ckpt = bt.run()

    assert active_workers["peak"] <= 2
    assert final_ckpt.status == "completed"
    assert final_ckpt.total_pages == 16
    assert final_ckpt.completed_pages == list(range(16))
    assert final_ckpt.pending_pages == []
    assert final_ckpt.eta_seconds == 0.0
    assert out_pdf.is_file()
    assert (work_dir / "qa_report.json").is_file()
    assert not list(work_dir.glob("*.tmp_*"))
    assert not list((work_dir / "batches").glob("*.tmp_*"))

    qa_data = json.loads((work_dir / "qa_report.json").read_text(encoding="utf-8"))
    assert qa_data["passed"] is True
    assert qa_data["summary"]["fail_count"] == 0

    qa_report = verify_translated_pdf(
        str(src_pdf),
        str(out_pdf),
        flagged_overflow_pages=final_ckpt.overflow_pages,
    )
    assert qa_report.passed is True
    assert qa_report.orig_pages == 16 and qa_report.out_pages == 16
    assert qa_report.toc_preserved is True


def test_linear_pdf_386p_two_batches_cancel_and_cross_restart_resume(tmp_path):
    assert LINEAR_PDF.is_file()
    out_pdf = tmp_path / "linear_vi.pdf"
    work_dir = tmp_path / "work_linear"

    phase1_calls: list[str] = []

    class Phase1Mock(OfflineViMockTranslator):
        def translate(self, text: str) -> str:
            phase1_calls.append(text)
            return super().translate(text)

    bt_holder: dict[str, PdfBatchTranslator] = {}

    def _cancel_after_2_batches(ckpt: BatchCheckpoint) -> None:
        if len(ckpt.completed_batches) >= 2:
            bt_holder["bt"].cancel()

    bt1 = PdfBatchTranslator(
        LINEAR_PDF,
        out_pdf,
        work_dir,
        engine="mock",
        max_workers=1,
        translator=Phase1Mock(),
        on_progress=_cancel_after_2_batches,
    )
    bt_holder["bt"] = bt1
    ckpt1 = bt1.run()

    assert ckpt1.status == "cancelled"
    assert ckpt1.completed_batches == [0, 1]
    assert ckpt1.total_pages == 386
    expected_p1_pages = ckpt1.batches[0] + ckpt1.batches[1]
    assert ckpt1.completed_pages == expected_p1_pages
    assert len(ckpt1.pending_pages) == 386 - len(expected_p1_pages)
    assert ckpt1.pages_per_sec > 0
    assert ckpt1.eta_seconds is not None and ckpt1.eta_seconds > 0

    # Verify partial.pdf is immediately readable and contains translated text
    partial_pdf = work_dir / "partial.pdf"
    assert partial_pdf.is_file()
    with pymupdf.open(str(partial_pdf)) as pdoc:
        assert len(pdoc) == len(expected_p1_pages)
        combined_txt = "\n".join(pdoc[i].get_text("text") for i in range(len(pdoc)))
        assert "Bản dịch" in combined_txt or "Định lý" in combined_txt or "các" in combined_txt

    ckpt_disk = json.loads((work_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert ckpt_disk["status"] == "cancelled"
    assert ckpt_disk["completed_batches"] == [0, 1]

    tm_verify = SQLiteTranslationMemory(work_dir / "pdf_tm.sqlite3")
    try:
        assert tm_verify.count() > 0
    finally:
        tm_verify.close()

    # Phase 2: Simulated process restart via from_checkpoint() -> resume() starts at batch 3 (idx 2)
    phase2_calls: list[str] = []

    class Phase2Mock(OfflineViMockTranslator):
        def translate(self, text: str) -> str:
            phase2_calls.append(text)
            return super().translate(text)

    bt2_holder: dict[str, PdfBatchTranslator] = {}
    batches_seen_in_phase2: list[int] = []

    def _stop_after_batch_3(ckpt: BatchCheckpoint) -> None:
        batches_seen_in_phase2.append(ckpt.completed_batches[-1])
        if len(ckpt.completed_batches) >= 3:
            bt2_holder["bt"].cancel()

    bt2 = PdfBatchTranslator.from_checkpoint(
        work_dir,
        translator=Phase2Mock(),
        on_progress=_stop_after_batch_3,
    )
    bt2_holder["bt"] = bt2
    ckpt2 = bt2.resume()

    assert ckpt2.completed_batches == [0, 1, 2]
    assert batches_seen_in_phase2 == [2]  # jumped directly to batch index 2 (3rd batch)!
    assert len(ckpt2.completed_pages) == len(ckpt2.batches[0] + ckpt2.batches[1] + ckpt2.batches[2])

    with pymupdf.open(str(partial_pdf)) as pdoc2:
        assert len(pdoc2) == len(ckpt2.completed_pages)

    # Zero re-translations of any segment already stored in pdf_tm.sqlite3 from batches 0-1
    phase1_set = set(phase1_calls)
    retranslated = [c for c in phase2_calls if c in phase1_set]
    assert retranslated == []


def test_r2_ambiguous_terms_glossary_context_prompt_leak_and_scan_policy(tmp_path):
    # 1. extract_ambiguous_terms with scan_pages_all=True finds terms past page 30
    many_pdf = tmp_path / "many35.pdf"
    doc = pymupdf.open()
    for i in range(35):
        p = doc.new_page(width=595, height=842)
        if i >= 32:
            p.insert_text((72, 100), "The trace of the operator has finite order and degree.", fontsize=11)
        else:
            p.insert_text((72, 100), f"Plain page {i + 1} with simple prose.", fontsize=11)
    doc.save(str(many_pdf))
    doc.close()

    with pymupdf.open(str(many_pdf)) as d_check:
        assert "trace" not in extract_ambiguous_terms(d_check, scan_pages_all=False)
        full_amb = extract_ambiguous_terms(d_check, scan_pages_all=True)
        assert "trace" in full_amb and "order" in full_amb and "degree" in full_amb

    insp = inspect_pdf(many_pdf, scan_pages_all=True)
    assert insp["total_pages"] == 35
    assert "trace" in insp["ambiguous_terms"]
    assert insp["default_glossary"] == DEFAULT_EN_VI_GLOSSARY

    # 2. Case-insensitive user glossary override, >8 glossary terms in LLM prompt,
    #    word-boundary check ('ring' != 'during'), cross-page neighbor context,
    #    cache-key isolation, and prompt-leak stripping.
    ctx_pdf = tmp_path / "ctx2.pdf"
    ctx_out = tmp_path / "ctx2_vi.pdf"
    cdoc = pymupdf.open()
    cp0 = cdoc.new_page(width=612, height=792)
    cp0.insert_text(
        (72, 100),
        "During the proof on page one, a matrix has an eigenvalue and eigenvector in a vector space.",
        fontsize=11,
    )
    cp1 = cdoc.new_page(width=612, height=792)
    cp1.insert_text(
        (72, 100),
        "Every theorem, lemma, corollary, proposition, matrix, subspace, basis, determinant, eigenvalue, eigenvector, and linear transformation satisfies the boundary condition.",
        fontsize=11,
    )
    cdoc.save(str(ctx_pdf))
    cdoc.close()

    llm_prompts: list[str] = []
    cache_keys_seen: list[str] = []
    tm_mem: dict[str, str] = {}

    class LeakyGeminiMock:
        engine = "gemini"

        def translate(self, text: str) -> str:
            llm_prompts.append(text)
            return (
                "Glossary: matrix = ma trận chuẩn\n"
                "[Context before - do NOT output]: leaked context\n"
                "[Context after - do NOT output]: leaked after\n"
                "[Translate ONLY this paragraph]:\n"
                "Bản dịch tiếng Việt sạch cho đoạn văn toán học với ma trận chuẩn."
            )

    def _c_lookup(src: str) -> str | None:
        cache_keys_seen.append(src)
        return tm_mem.get(src)

    def _c_store(src: str, val: str) -> None:
        tm_mem[src] = val

    user_glossary = {
        "Matrix": "ma trận chuẩn",
        "ring": "vành đại số",
    }
    translate_pdf_file(
        str(ctx_pdf),
        str(ctx_out),
        translator=LeakyGeminiMock(),  # type: ignore[arg-type]
        glossary=user_glossary,
        cache_lookup=_c_lookup,
        cache_store=_c_store,
    )

    assert len(llm_prompts) == 2
    p0_prompt, p1_prompt = llm_prompts[0], llm_prompts[1]
    assert "[Context after - do NOT output]:" in p0_prompt
    assert "Every theorem" in p0_prompt
    assert "[Context before - do NOT output]:" in p1_prompt
    assert "During the proof" in p1_prompt

    # Case-insensitive override & word boundary ('ring' does not match 'During')
    assert "Matrix = ma trận chuẩn" in p0_prompt
    assert "matrix = ma trận;" not in p0_prompt.lower()
    assert "ring = vành đại số" not in p0_prompt

    # >8 glossary terms all injected into LLM prompt on page 1
    glossary_line = [ln for ln in p1_prompt.splitlines() if ln.startswith("Glossary:")][0]
    assert glossary_line.count("=") >= 10

    # Cache keys and stored TM values must NEVER contain prompt context headers
    assert all("[Context" not in k and "Glossary:" not in k for k in cache_keys_seen)
    assert tm_mem
    assert all(
        "[Context" not in v and "[Translate ONLY" not in v and "Glossary:" not in v
        for v in tm_mem.values()
    )
    # Emitted [Context before/after...] headers must be normalized to single lines
    ctx_lines = [
        ln
        for pr in llm_prompts
        for ln in pr.splitlines()
        if ln.startswith("[Context ")
    ]
    assert len(ctx_lines) == 2
    assert all("\r" not in ln and "\n" not in ln for ln in ctx_lines)

    # Prompt leaks must be stripped from the output PDF
    with pymupdf.open(str(ctx_out)) as odoc:
        full_out = re.sub(r"\s+", " ", odoc[0].get_text("text") + "\n" + odoc[1].get_text("text"))
        assert "[Context" not in full_out
        assert "[Translate ONLY" not in full_out
        assert "Glossary:" not in full_out
        assert "ma trận chuẩn" in full_out.lower()

    # 3. scan_policy: abort vs keep_original vs skip (both translate_pdf_file & PdfBatchTranslator)
    scan_mix_pdf = tmp_path / "mix3.pdf"
    _build_scan_plus_text_pdf(scan_mix_pdf)

    with pytest.raises(ScanPagesDetectedError):
        PdfBatchTranslator(
            scan_mix_pdf,
            tmp_path / "abort.pdf",
            tmp_path / "w_abort",
            scan_policy="abort",
            translator=OfflineViMockTranslator(),
        ).run()

    out_keep = tmp_path / "keep3.pdf"
    res_keep = translate_pdf_file(
        str(scan_mix_pdf),
        str(out_keep),
        scan_policy="keep_original",
        translator=OfflineViMockTranslator(),
    )
    assert res_keep.total_pages == 3

    out_skip = tmp_path / "skip3.pdf"
    res_skip = translate_pdf_file(
        str(scan_mix_pdf),
        str(out_skip),
        scan_policy="skip",
        translator=OfflineViMockTranslator(),
    )
    assert res_skip.total_pages == 2
    with pymupdf.open(str(out_skip)) as sdoc:
        assert len(sdoc) == 2
        toc_pages = [entry[2] for entry in sdoc.get_toc()]
        assert toc_pages == [1, 2]  # Chapter 2 remapped from page 3 -> page 2

    qa_skip = verify_translated_pdf(
        str(scan_mix_pdf),
        str(out_skip),
        require_page_count_match=False,
        page_pairs=[(0, 0), (2, 1)],
    )
    assert qa_skip.passed is True
    assert qa_skip.page_count_match is False

    # Batch translator with scan_policy="skip"
    out_batch_skip = tmp_path / "batch_skip.pdf"
    work_skip = tmp_path / "work_skip"
    ckpt_skip = PdfBatchTranslator(
        scan_mix_pdf,
        out_batch_skip,
        work_skip,
        scan_policy="skip",
        max_workers=1,
        translator=OfflineViMockTranslator(),
    ).run()
    assert ckpt_skip.status == "completed"
    assert ckpt_skip.scan_pages == [1]
    with pymupdf.open(str(out_batch_skip)) as bsdoc:
        assert len(bsdoc) == 2

    # 4. Page dimension mismatch triggers FAIL in verify_translated_pdf
    bad_dim_pdf = tmp_path / "bad_dim.pdf"
    with pymupdf.open(str(ctx_out)) as d_mod:
        d_mod[0].set_mediabox(pymupdf.Rect(0, 0, 500, 700))
        d_mod.save(str(bad_dim_pdf))
    qa_bad_dim = verify_translated_pdf(str(ctx_pdf), str(bad_dim_pdf))
    assert qa_bad_dim.passed is False
    assert qa_bad_dim.pages[0].status == "FAIL"
    assert any("Page size mismatch" in n for n in qa_bad_dim.pages[0].notes)


def test_redact_rects_coalescing_does_not_bridge_large_horizontal_gap(tmp_path):
    """Regression: y0 jitter across >24pt horizontal gaps must not merge and redact middle formulas."""
    pdf_path = tmp_path / "jitter_gap.pdf"
    out_path = tmp_path / "jitter_gap_vi.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # Left prose slightly lower (larger y0) than right prose so (y0, x0) ordering puts right span first.
    page.insert_text((50, 100.3), "Left prose before gap.", fontsize=11)
    page.insert_text((300, 100.0), "continue prose on right.", fontsize=11)
    # Inline math sitting inside the >24pt gap (x=215) with no overlap with left/right prose.
    page.insert_text((215, 105.0), "f(x) = \u2211 \u03b1_i", fontsize=12)
    # Adjacent same-line spans within 24pt that SHOULD still coalesce.
    page.insert_text((50, 160.0), "Adjacent", fontsize=11)
    page.insert_text((100, 160.2), "spans.", fontsize=11)
    doc.save(str(pdf_path))
    doc.close()

    with pymupdf.open(str(pdf_path)) as d_chk:
        plans, preserved_cnt, inline_cnt = _plan_page_blocks(d_chk[0])
        assert len(plans) >= 1
        assert preserved_cnt + inline_cnt >= 1
        all_redacts = [rr for pl in plans for rr in pl.redact_rects]
        # Must NEVER create a rect bridging across the left/right >24pt gap (50..160 to 300..420)
        assert not any(rr.x0 < 180.0 and rr.x1 > 290.0 for rr in all_redacts)
        # Adjacent spans at y~160 (gap < 24pt) should coalesce into one rect
        adj_rects = [rr for rr in all_redacts if rr.y0 > 140.0 and rr.y1 < 175.0]
        assert len(adj_rects) == 1
        assert adj_rects[0].x0 <= 55.0 and adj_rects[0].x1 >= 120.0

    translate_pdf_file(
        str(pdf_path),
        str(out_path),
        translator=OfflineViMockTranslator(),  # type: ignore[arg-type]
    )
    qa = verify_translated_pdf(str(pdf_path), str(out_path))
    assert qa.passed is True
    assert qa.pages[0].formula_similarity >= 0.94


def test_strip_llm_prompt_leaks_handles_markdown_headers(tmp_path):
    """Regression: markdown-wrapped echoed prompt headers are stripped cleanly."""
    leaked_a = (
        "**Glossary:** matrix = ma trận\n"
        "* [Context before - do NOT output]: previous sentence\n"
        "- [Context after - do NOT output]: next sentence\n"
        "Bản dịch sạch."
    )
    assert _strip_llm_prompt_leaks(leaked_a) == "Bản dịch sạch."

    leaked_b = (
        "- **Glossary**: matrix = ma trận\n"
        "> [Context before]: x\n"
        "### [Context after]: y\n"
        "**[Translate ONLY this paragraph]:**\n"
        "Kết quả chuẩn."
    )
    assert _strip_llm_prompt_leaks(leaked_b) == "Kết quả chuẩn."

    leaked_c = (
        "1. ***Glossary:*** matrix = ma trận\n"
        "***[Translate ONLY this paragraph]:*** Chỉ giữ đoạn này."
    )
    assert _strip_llm_prompt_leaks(leaked_c) == "Chỉ giữ đoạn này."

    # Colonless & colon-variant markdown-wrapped [Translate ONLY this paragraph] and [Context before/after ...]
    for delim in ("**", "*", "***", "__", "_", "___", "`", "```"):
        assert (
            _strip_llm_prompt_leaks(f"{delim}[Translate ONLY this paragraph]{delim}\nBản dịch chuẩn.")
            == "Bản dịch chuẩn."
        )
        assert (
            _strip_llm_prompt_leaks(f"{delim}[Translate ONLY this paragraph]:{delim}\nBản dịch chuẩn.")
            == "Bản dịch chuẩn."
        )
        assert (
            _strip_llm_prompt_leaks(f"{delim}[Translate ONLY this paragraph]{delim}:\nBản dịch chuẩn.")
            == "Bản dịch chuẩn."
        )
        assert (
            _strip_llm_prompt_leaks(
                f"{delim}[Context before - do NOT output]{delim} prev text\n"
                f"{delim}[Context after - do NOT output]{delim} next text\n"
                "Bản dịch chuẩn."
            )
            == "Bản dịch chuẩn."
        )
    assert (
        _strip_llm_prompt_leaks(
            "**[Context before - do NOT output]** prev text\n"
            "**[Translate ONLY this paragraph]**\n"
            "**[Context after - do NOT output]** next text\n"
            "Bản dịch sạch."
        )
        == "Bản dịch sạch."
    )
    assert (
        _strip_llm_prompt_leaks("** [Translate ONLY this paragraph] ** : \nBản dịch.")
        == "Bản dịch."
    )

    # Unwrapped [Translate ONLY this paragraph] (with/without ':') before markdown-formatted prose
    unwrapped_md_cases = [
        ("[Translate ONLY this paragraph]\n**Bản dịch chuẩn.**", "**Bản dịch chuẩn.**"),
        ("[Translate ONLY this paragraph]: **Bản dịch chuẩn.**", "**Bản dịch chuẩn.**"),
        ("[Translate ONLY this paragraph]:\n**Bản dịch chuẩn.**", "**Bản dịch chuẩn.**"),
        ("[Translate ONLY this paragraph]\n*Lưu ý:* Bản dịch chuẩn.", "*Lưu ý:* Bản dịch chuẩn."),
        ("[Translate ONLY this paragraph]: *Lưu ý:* Ok.", "*Lưu ý:* Ok."),
        ("[Translate ONLY this paragraph]\n* Mục 1\n* Mục 2", "* Mục 1\n* Mục 2"),
        ("[Translate ONLY this paragraph]\n* Định lý 1: Kết quả.", "* Định lý 1: Kết quả."),
        ("[Translate ONLY this paragraph]\n__Định lý.__", "__Định lý.__"),
        ("[Translate ONLY this paragraph]\n`code` là biến.", "`code` là biến."),
        ("[Translate ONLY this paragraph]: `code`", "`code`"),
        ("[Translate ONLY this paragraph]\n- Mục 1", "- Mục 1"),
        ("[Translate ONLY this paragraph]\n1. Mục 1", "1. Mục 1"),
        ("[Translate ONLY this paragraph]\n> Trích dẫn", "> Trích dẫn"),
        ("[Translate ONLY this paragraph]\n### Tiêu đề", "### Tiêu đề"),
    ]
    for raw_in, expected in unwrapped_md_cases:
        assert _strip_llm_prompt_leaks(raw_in) == expected

    # Wrapped tags followed by markdown prose on same line or newline (must not strip prose delimiters)
    wrapped_md_cases = [
        ("**[Translate ONLY this paragraph]**\n**Bản dịch.**", "**Bản dịch.**"),
        ("**[Translate ONLY this paragraph]:**\n**Bản dịch.**", "**Bản dịch.**"),
        ("**[Translate ONLY this paragraph]: **Bản dịch.**", "**Bản dịch.**"),
        ("**[Translate ONLY this paragraph] **Bản dịch.**", "**Bản dịch.**"),
        ("**[Translate ONLY this paragraph]: __Định lý.__", "__Định lý.__"),
        ("`[Translate ONLY this paragraph]`: `code`", "`code`"),
        ("`[Translate ONLY this paragraph]: `code`", "`code`"),
        ("* [Translate ONLY this paragraph]: **Bản dịch.**", "**Bản dịch.**"),
        ("* [Translate ONLY this paragraph]: * Mục 1", "* Mục 1"),
        ("* [Translate ONLY this paragraph]: *Ghi chú:* Ok.", "*Ghi chú:* Ok."),
        ("* [Translate ONLY this paragraph]\n**Bản dịch.**", "**Bản dịch.**"),
        ("_ [Translate ONLY this paragraph]: __Định lý.__", "__Định lý.__"),
        ("* [Translate ONLY this paragraph] * : Bản dịch.", "Bản dịch."),
        # Mismatched / unclosed wrappers
        ("**[Translate ONLY this paragraph]*\nBản dịch.", "Bản dịch."),
        ("*[Translate ONLY this paragraph]**\nBản dịch.", "Bản dịch."),
        ("*[Translate ONLY this paragraph]:**\nBản dịch.", "Bản dịch."),
        ("**[Translate ONLY this paragraph]__\nBản dịch.", "Bản dịch."),
        ("**[Translate ONLY this paragraph]\n**Bản dịch.**", "**Bản dịch.**"),
        # Standalone next-line colons
        ("[Translate ONLY this paragraph]\n:\nBản dịch.", "Bản dịch."),
        ("**[Translate ONLY this paragraph]**\n: Bản dịch.", "Bản dịch."),
        ("[Translate ONLY this paragraph]\n: **Bản dịch.**", "**Bản dịch.**"),
        ("**[Translate ONLY this paragraph]**\n:\n**Bản dịch.**", "**Bản dịch.**"),
        # Backtick, '+' bullet, and bold-wrapped Glossary / Context header lines
        ("`[Context before - do NOT output]: prev`\nBản dịch.", "Bản dịch."),
        ("`[Context before - do NOT output]` prev\nBản dịch.", "Bản dịch."),
        ("`[Context before - do NOT output]:` prev\nBản dịch.", "Bản dịch."),
        ("`Glossary:` matrix = ma trận\nBản dịch.", "Bản dịch."),
        ("`Glossary`: matrix = ma trận\nBản dịch.", "Bản dịch."),
        ("`Glossary: matrix = ma trận`\nBản dịch.", "Bản dịch."),
        ("+ **Glossary:** matrix = ma trận\nBản dịch.", "Bản dịch."),
        ("**Glossary: matrix = ma trận**\nBản dịch.", "Bản dịch."),
        ("**Glossary :** matrix = ma trận\nBản dịch.", "Bản dịch."),
        ("**Glossary** : matrix = ma trận\nBản dịch.", "Bản dịch."),
        ("**[Context before - do NOT output]: prev text**\nBản dịch.", "Bản dịch."),
    ]
    for raw_in, expected in wrapped_md_cases:
        assert _strip_llm_prompt_leaks(raw_in) == expected

    # Normal Vietnamese prose false-positive guards (must NOT be stripped)
    safe_cases = [
        "**Glossary**",
        "**Glossary** chứa các thuật ngữ toán học.",
        "`Glossary` là danh sách từ.",
        "`Glossary` là mục tham khảo.",
        "* **Glossary** là bảng thuật ngữ.",
        "### Glossary",
        "Trong phần Glossary của tài liệu, ma trận được định nghĩa rõ.",
        "Xem mục (Glossary) ở cuối sách.",
        "1. Định lý cơ bản của đại số tuyến tính.",
        "- Thuộc tính trực giao của ma trận.",
        "> Ghi chú quan trọng về vector riêng.",
        "### Kết quả chính",
        "**Chú ý:** đây là ghi chú bình thường.",
        "*Lưu ý:* không bỏ qua điều kiện biên.",
        "* Định lý 1: **Kết quả.**",
        "1. **Định nghĩa:** không gian vector.",
        "Context before: giá trị trước đó vẫn giữ nguyên.",
        "**Context before:** giá trị trước đó vẫn giữ nguyên.",
        "Glossary**: không xóa.",
        "-" * 100,
    ]
    for safe_txt in safe_cases:
        assert _strip_llm_prompt_leaks(safe_txt) == safe_txt

    # ReDoS sanity: repeated punctuation/prefix characters must resolve in linear time
    t0 = time.perf_counter()
    for adv in ("-" * 200, "*" * 200, "#" * 200 + " Tiêu đề", "- " * 200 + "x"):
        assert _LLM_PROMPT_HEADER_LINE_RE.match(adv) is None
    assert (time.perf_counter() - t0) < 0.05

    pdf_path = tmp_path / "md_leak.pdf"
    out_path = tmp_path / "md_leak_vi.pdf"
    doc = pymupdf.open()
    p0 = doc.new_page(width=595, height=842)
    p0.insert_text((72, 100), "A matrix has an eigenvalue.", fontsize=11)
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 100), "The eigenvector is orthogonal.", fontsize=11)
    doc.save(str(pdf_path))
    doc.close()

    class MarkdownLeakyMock:
        engine = "gemini"

        def translate(self, text: str) -> str:
            target = text.split("[Translate ONLY this paragraph]:")[-1]
            if "eigenvalue" in target:
                return (
                    "**Glossary:** matrix = ma trận\n"
                    "- [Context before - do NOT output]: leaked prev\n"
                    "[Translate ONLY this paragraph]:\n"
                    "**Định lý 1.** Cho ma trận chuẩn."
                )
            return (
                "**[Context before - do NOT output]** prev\n"
                "`[Translate ONLY this paragraph]`\n"
                "*Lưu ý:* Kết quả."
            )

    translate_pdf_file(
        str(pdf_path),
        str(out_path),
        translator=MarkdownLeakyMock(),  # type: ignore[arg-type]
    )
    with pymupdf.open(str(out_path)) as odoc:
        out_txt = re.sub(r"\s+", " ", "\n".join(odoc[i].get_text("text") for i in range(len(odoc))))
        assert "Glossary" not in out_txt
        assert "[Context" not in out_txt
        assert "[Translate ONLY" not in out_txt
        assert "**Định lý 1.**" in out_txt
        assert "*Lưu ý:*" in out_txt
        assert "ma trận" in out_txt.lower()

    # PdfBatchTranslator TM persistence with leaky markdown-prose mock: TM stores stripped text & resume makes 0 calls
    batch_work = tmp_path / "md_batch_work"
    batch_out = tmp_path / "md_batch_out.pdf"

    class CountingLeakyGeminiMock(MarkdownLeakyMock):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def translate(self, text: str) -> str:
            self.calls.append(text)
            return super().translate(text)

    tr_b1 = CountingLeakyGeminiMock()
    ckpt1 = PdfBatchTranslator(
        pdf_path,
        batch_out,
        batch_work,
        engine="gemini",
        max_workers=1,
        translator=tr_b1,  # type: ignore[arg-type]
    ).run()
    assert ckpt1.status == "completed"
    assert len(tr_b1.calls) == 2

    tm_db = SQLiteTranslationMemory(batch_work / "pdf_tm.sqlite3")
    try:
        ghash = compute_glossary_hash(None)
        v0 = tm_db.lookup("A matrix has an eigenvalue.", ghash, "gemini", "en", "vi")
        v1 = tm_db.lookup("The eigenvector is orthogonal.", ghash, "gemini", "en", "vi")
        assert v0 == "**Định lý 1.** Cho ma trận chuẩn."
        assert v1 == "*Lưu ý:* Kết quả."
    finally:
        tm_db.close()

    # Simulate partial checkpoint rollback to batch 0 -> resume hits TM with 0 re-translations
    ckpt_path = batch_work / "checkpoint.json"
    ckpt_data = json.loads(ckpt_path.read_text(encoding="utf-8"))
    ckpt_data["completed_batches"] = []
    ckpt_data["completed_pages"] = []
    ckpt_data["pending_pages"] = [0, 1]
    ckpt_data["status"] = "cancelled"
    ckpt_path.write_text(json.dumps(ckpt_data), encoding="utf-8")

    tr_b2 = CountingLeakyGeminiMock()
    resumed = PdfBatchTranslator.from_checkpoint(
        batch_work,
        translator=tr_b2,  # type: ignore[arg-type]
        max_workers=1,
    ).resume()
    assert resumed.status == "completed"
    assert len(tr_b2.calls) == 0


def test_scan_policy_skip_normalizes_toc_hierarchy(tmp_path):
    """Regression: skipping SCAN pages with parent TOC entries normalizes surviving sub-entries."""
    pdf_path = tmp_path / "mixed_toc_skip.pdf"
    doc = pymupdf.open()

    img_doc = pymupdf.open()
    ip = img_doc.new_page(width=400, height=600)
    ip.draw_rect(pymupdf.Rect(0, 0, 400, 600), color=(0.2, 0.2, 0.2), fill=(0.85, 0.85, 0.85))
    pix = ip.get_pixmap(dpi=72)
    img_doc.close()

    # Page 0: SCAN page holding level-1 TOC entry
    p0 = doc.new_page(width=595, height=842)
    p0.insert_image(pymupdf.Rect(20, 20, 575, 822), pixmap=pix)

    # Page 1: TEXT page holding level-2 and level-3 TOC entries
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 100), "Subchapter 2.1 discusses matrix rank.", fontsize=11)

    # Page 2: SCAN page holding level-1 and level-2 TOC entries
    p2 = doc.new_page(width=595, height=842)
    p2.insert_image(pymupdf.Rect(20, 20, 575, 822), pixmap=pix)

    # Page 3: TEXT page holding level-1 and level-3 (after skipped lvl-2 on page 2 -> jump 1->3)
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text((72, 100), "Chapter 4 discusses vector space basis.", fontsize=11)

    doc.set_toc([
        [1, "Scan Chapter 1", 1],
        [2, "Subchapter 2.1", 2],
        [3, "Detail 2.1.1", 2],
        [1, "Chapter 4", 4],
        [2, "Scan Sec 4.1", 3],
        [3, "Jump 4.1.1", 4],
    ])
    doc.save(str(pdf_path))
    doc.close()

    expected_toc = [
        [1, "Subchapter 2.1", 1],
        [2, "Detail 2.1.1", 1],
        [1, "Chapter 4", 2],
        [2, "Jump 4.1.1", 2],
    ]

    out_file = tmp_path / "mixed_toc_file_skip.pdf"
    res = translate_pdf_file(
        str(pdf_path),
        str(out_file),
        scan_policy="skip",
        translator=OfflineViMockTranslator(),  # type: ignore[arg-type]
    )
    assert res.total_pages == 2
    with pymupdf.open(str(out_file)) as fdoc:
        assert len(fdoc) == 2
        assert fdoc.get_toc() == expected_toc

    out_batch = tmp_path / "mixed_toc_batch_skip.pdf"
    ckpt = PdfBatchTranslator(
        pdf_path,
        out_batch,
        tmp_path / "work_toc_skip",
        scan_policy="skip",
        max_workers=1,
        translator=OfflineViMockTranslator(),
    ).run()
    assert ckpt.status == "completed"
    with pymupdf.open(str(out_batch)) as bdoc:
        assert len(bdoc) == 2
        assert bdoc.get_toc() == expected_toc
