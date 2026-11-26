"""Tier 1 E2E Feature Tests: >= 5 focused tests per feature area F1-F8 (>= 40 total)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import re
import threading
import time

import pymupdf
import pytest


def _collect_tk_texts(root) -> list[str]:
    texts: list[str] = []
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            txt = w.cget("text")
            if isinstance(txt, str) and txt.strip():
                texts.append(txt.strip())
        except Exception:
            pass
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return texts


def _collect_tk_widgets(root) -> list:
    widgets = []
    stack = [root]
    while stack:
        w = stack.pop()
        widgets.append(w)
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return widgets


# ---------------------------------------------------------------------------
# F1: Density-Adaptive Batch Chunking & Bounded Queue / Retry
# ---------------------------------------------------------------------------


def test_f1_sparse_pdf_uses_larger_batches_up_to_10(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "sparse.pdf", num_pages=20, dense=False)
    out = tmp_path / "sparse_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=2)
    ckpt = bt.run()

    assert ckpt.status == "completed"
    assert ckpt.total_pages == 20
    flat_pages = [p for batch in ckpt.batches for p in batch]
    assert sorted(flat_pages) == list(range(20))
    assert len(flat_pages) == 20
    # Sparse 20p PDF should batch near upper bound (8-10 pages/batch, never > 10)
    assert all(1 <= len(b) <= 10 for b in ckpt.batches)
    assert max(len(b) for b in ckpt.batches) >= 8


def test_f1_dense_pdf_uses_smaller_batches_around_5(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "dense.pdf", num_pages=15, dense=True)
    out = tmp_path / "dense_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=2)
    ckpt = bt.run()

    assert ckpt.status == "completed"
    assert sorted(p for b in ckpt.batches for p in b) == list(range(15))
    # Dense pages should trigger smaller batches (5-7 pages, <= 10)
    assert len(ckpt.batches) >= 2
    assert all(5 <= len(b) <= 10 for b in ckpt.batches[:-1])
    assert min(len(b) for b in ckpt.batches[:-1]) <= 6


def test_f1_bounded_concurrency_respects_max_workers(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "conc.pdf", num_pages=20, dense=True)
    out = tmp_path / "conc_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock", per_call_delay=0.02)

    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=2)
    ckpt = bt.run()

    assert ckpt.status == "completed"
    assert 1 <= tr.max_active_calls <= 2


def test_f1_retry_recovers_from_http_429_rate_limit(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from .conftest import make_http_error

    src = pdf_factory.make_text_pdf(tmp_path / "rate429.pdf", num_pages=2, dense=False)
    out = tmp_path / "rate429_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(
        engine="mock",
        fail_on_attempts={1: make_http_error(429), 2: make_http_error(429)},
    )

    bt = PdfBatchTranslator(
        src, out, work, translator=tr, max_workers=1, max_retries=4, base_backoff=0.01
    )
    ckpt = bt.run()

    assert ckpt.status == "completed"
    assert out.is_file()
    assert tr.attempt_count >= tr.call_count + 2


def test_f1_retry_recovers_from_transient_timeout(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "timeout.pdf", num_pages=2, dense=False)
    out = tmp_path / "timeout_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(
        engine="mock",
        fail_on_attempts={1: TimeoutError("Transient read timeout")},
    )

    bt = PdfBatchTranslator(
        src, out, work, translator=tr, max_workers=1, max_retries=3, base_backoff=0.01
    )
    ckpt = bt.run()

    assert ckpt.status == "completed"
    assert out.is_file()
    assert tr.attempt_count >= 2


def test_f1_on_progress_callback_emits_monotonic_updates(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "prog.pdf", num_pages=12, dense=True)
    out = tmp_path / "prog_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    seen_completed_counts: list[int] = []

    def on_progress(ckpt):
        seen_completed_counts.append(len(ckpt.completed_pages))

    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=1, on_progress=on_progress)
    ckpt = bt.run()

    assert ckpt.status == "completed"
    assert len(seen_completed_counts) >= 2
    assert seen_completed_counts == sorted(seen_completed_counts)
    assert seen_completed_counts[-1] == 12


# ---------------------------------------------------------------------------
# F2: SQLite Translation Memory (pdf_tm.sqlite3)
# ---------------------------------------------------------------------------


def test_f2_tm_make_key_sha256_and_field_sensitivity(tmp_path):
    from mathtranslate.pdf_batch import SQLiteTranslationMemory

    tm = SQLiteTranslationMemory(tmp_path / "pdf_tm.sqlite3")
    try:
        k_base = tm.make_key("Hello matrix", "h1", "google", "en", "vi")
        assert re.fullmatch(r"[a-f0-9]{64}", k_base)
        assert k_base == tm.make_key("Hello matrix", "h1", "google", "en", "vi")
        assert k_base != tm.make_key("Hello matrix!", "h1", "google", "en", "vi")
        assert k_base != tm.make_key("Hello matrix", "h2", "google", "en", "vi")
        assert k_base != tm.make_key("Hello matrix", "h1", "openai", "en", "vi")
        assert k_base != tm.make_key("Hello matrix", "h1", "google", "fr", "vi")
        assert k_base != tm.make_key("Hello matrix", "h1", "google", "en", "ja")
    finally:
        tm.close()


def test_f2_tm_store_lookup_count_persists_across_reopen(tmp_path):
    from mathtranslate.pdf_batch import SQLiteTranslationMemory

    db_path = tmp_path / "pdf_tm.sqlite3"
    tm = SQLiteTranslationMemory(db_path)
    try:
        assert tm.count() == 0
        tm.store("Every matrix has an eigenvalue", "Mọi ma trận đều có giá trị riêng", "gh1", "google", "en", "vi")
        assert tm.count() == 1
        assert tm.lookup("Every matrix has an eigenvalue", "gh1", "google", "en", "vi") == "Mọi ma trận đều có giá trị riêng"
        assert tm.lookup("Missing text", "gh1", "google", "en", "vi") is None
    finally:
        tm.close()

    tm2 = SQLiteTranslationMemory(db_path)
    try:
        assert tm2.count() == 1
        assert tm2.lookup("Every matrix has an eigenvalue", "gh1", "google", "en", "vi") == "Mọi ma trận đều có giá trị riêng"
    finally:
        tm2.close()


def test_f2_tm_thread_safe_concurrent_access(tmp_path):
    from mathtranslate.pdf_batch import SQLiteTranslationMemory

    tm = SQLiteTranslationMemory(tmp_path / "pdf_tm.sqlite3")
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            for i in range(25):
                src = f"Thread {tid} segment {i}"
                dst = f"Đoạn {tid}-{i}"
                tm.store(src, dst, "gh", "google", "en", "vi")
                got = tm.lookup(src, "gh", "google", "en", "vi")
                assert got == dst
        except BaseException as exc:
            errors.append(exc)

    try:
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors
        assert tm.count() == 200
    finally:
        tm.close()


def test_f2_batch_translator_populates_tm_and_reuses_cache(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator, SQLiteTranslationMemory

    src = pdf_factory.make_text_pdf(tmp_path / "tm_src.pdf", num_pages=3, dense=False)
    work = tmp_path / "work"

    tr1 = make_translator(engine="mock")
    bt1 = PdfBatchTranslator(src, tmp_path / "out1.pdf", work, translator=tr1)
    bt1.run()
    assert tr1.call_count > 0

    tm = SQLiteTranslationMemory(work / "pdf_tm.sqlite3")
    try:
        assert tm.count() > 0
    finally:
        tm.close()

    tr2 = make_translator(engine="mock")
    bt2 = PdfBatchTranslator(src, tmp_path / "out2.pdf", work, translator=tr2)
    ckpt2 = bt2.run()
    assert ckpt2.status == "completed"
    assert tr2.call_count == 0


def test_f2_tm_cache_miss_when_engine_or_language_changes(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "tm_miss.pdf", num_pages=2, dense=False)
    work = tmp_path / "work"

    tr1 = make_translator(engine="mock")
    PdfBatchTranslator(
        src, tmp_path / "out1.pdf", work, engine="google", l_from="en", l_to="vi", translator=tr1
    ).run()
    assert tr1.call_count > 0

    tr2 = make_translator(engine="openai")
    PdfBatchTranslator(
        src, tmp_path / "out2.pdf", work, engine="openai", l_from="en", l_to="vi", translator=tr2
    ).run()
    assert tr2.call_count > 0

    tr3 = make_translator(engine="mock")
    PdfBatchTranslator(
        src, tmp_path / "out3.pdf", work, engine="google", l_from="fr", l_to="vi", translator=tr3
    ).run()
    assert tr3.call_count > 0

    tr4 = make_translator(engine="mock")
    PdfBatchTranslator(
        src, tmp_path / "out4.pdf", work, engine="google", l_from="en", l_to="ja", translator=tr4
    ).run()
    assert tr4.call_count > 0


# ---------------------------------------------------------------------------
# F3: Atomic Checkpointing & Progressive partial.pdf
# ---------------------------------------------------------------------------


def test_f3_checkpoint_json_contains_all_contract_fields(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "ckpt_fields.pdf", num_pages=6, dense=False)
    out = tmp_path / "out.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    PdfBatchTranslator(src, out, work, translator=tr, job_id="job_e2e_f3").run()

    ckpt_file = work / "checkpoint.json"
    assert ckpt_file.is_file()
    data = json.loads(ckpt_file.read_text(encoding="utf-8"))
    expected_keys = {
        "job_id",
        "input_path",
        "output_path",
        "partial_path",
        "tm_path",
        "engine",
        "l_from",
        "l_to",
        "scan_policy",
        "glossary_hash",
        "glossary",
        "status",
        "total_pages",
        "batches",
        "completed_batches",
        "completed_pages",
        "pending_pages",
        "overflow_pages",
        "scan_pages",
        "error_pages",
        "pages_per_sec",
        "eta_seconds",
        "elapsed_seconds",
        "updated_at",
    }
    assert expected_keys.issubset(data.keys())
    assert data["job_id"] == "job_e2e_f3"
    assert data["status"] == "completed"


def test_f3_partial_pdf_is_readable_after_each_batch(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "partial_check.pdf", num_pages=12, dense=True)
    out = tmp_path / "out.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    mid_run_partial_pages: list[int] = []

    def on_progress(ckpt):
        if ckpt.completed_batches and ckpt.pending_pages:
            p_path = Path(ckpt.partial_path)
            assert p_path.is_file()
            with pymupdf.open(str(p_path)) as doc:
                mid_run_partial_pages.append(len(doc))

    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=1, on_progress=on_progress)
    ckpt = bt.run()

    assert ckpt.status == "completed"
    assert mid_run_partial_pages and all(n > 0 for n in mid_run_partial_pages)


def test_f3_checkpoint_tracks_completed_pending_and_eta(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "eta.pdf", num_pages=6, dense=False)
    out = tmp_path / "out.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock", per_call_delay=0.005)

    ckpt = PdfBatchTranslator(src, out, work, translator=tr).run()
    assert ckpt.completed_pages == list(range(6))
    assert ckpt.pending_pages == []
    assert ckpt.pages_per_sec > 0
    assert ckpt.elapsed_seconds >= 0
    assert ckpt.eta_seconds is not None and ckpt.eta_seconds <= 0.01


def test_f3_overflow_pages_recorded_in_checkpoint(tmp_path, pdf_factory):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "tight.pdf", num_pages=1, tight_box=True)
    out = tmp_path / "tight_vi.pdf"
    work = tmp_path / "work"

    class VerboseTranslator:
        engine = "mock"
        language_from = "en"
        language_to = "vi"

        def translate(self, text: str) -> str:
            return (
                "Đây là một bản dịch tiếng Việt rất dài vượt quá chiều cao khung chữ chật hẹp "
                "để buộc bộ sắp chữ phải thu nhỏ cỡ chữ và đánh dấu trang tràn khung HOÀN_TẤT."
            )

    ckpt = PdfBatchTranslator(src, out, work, min_scale=0.85, translator=VerboseTranslator()).run()
    assert 0 in ckpt.overflow_pages
    saved = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
    assert 0 in saved["overflow_pages"]


def test_f3_qa_report_json_written_on_completion(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "qa.pdf", num_pages=2, dense=False)
    out = tmp_path / "qa_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    PdfBatchTranslator(src, out, work, translator=tr).run()
    qa_path = work / "qa_report.json"
    assert qa_path.is_file()
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
    assert qa_data["passed"] is True
    assert qa_data["orig_pages"] == 2
    assert qa_data["out_pages"] == 2


# ---------------------------------------------------------------------------
# F4: Cancel & Cross-Restart Resume
# ---------------------------------------------------------------------------


def test_f4_cancel_sets_cancelled_status_and_preserves_partial_pdf(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "cancel_src.pdf", num_pages=16, dense=True)
    out = tmp_path / "cancel_out.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}
    batches_done: list[int] = []

    def on_progress(ckpt):
        batches_done.append(len(ckpt.completed_batches))
        if len(ckpt.completed_batches) >= 1:
            holder["bt"].cancel()

    tr = make_translator(engine="mock")
    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=1, on_progress=on_progress)
    holder["bt"] = bt
    ckpt = bt.run()

    assert ckpt.status == "cancelled"
    assert len(ckpt.completed_batches) >= 1
    assert len(ckpt.pending_pages) > 0
    partial_path = work / "partial.pdf"
    assert partial_path.is_file()
    with pymupdf.open(str(partial_path)) as doc:
        assert len(doc) >= len(ckpt.completed_pages)


def test_f4_from_checkpoint_reloads_state_across_restart(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "reload.pdf", num_pages=16, dense=True)
    out = tmp_path / "reload_out.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def on_progress(ckpt):
        if len(ckpt.completed_batches) >= 1:
            holder["bt"].cancel()

    bt1 = PdfBatchTranslator(
        src,
        out,
        work,
        engine="mock",
        glossary={"field": "trường đại số"},
        translator=make_translator(engine="mock"),
        max_workers=1,
        on_progress=on_progress,
    )
    holder["bt"] = bt1
    ckpt1 = bt1.run()
    assert ckpt1.status == "cancelled"

    tr2 = make_translator(engine="mock")
    bt2 = PdfBatchTranslator.from_checkpoint(work, translator=tr2)
    assert bt2.checkpoint.completed_batches == ckpt1.completed_batches
    assert bt2.checkpoint.completed_pages == ckpt1.completed_pages
    assert bt2.checkpoint.glossary == {"field": "trường đại số"}


def test_f4_resume_continues_from_exact_interrupted_batch(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "resume_exact.pdf", num_pages=16, dense=True)
    out = tmp_path / "resume_out.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def on_progress(ckpt):
        if len(ckpt.completed_batches) >= 1:
            holder["bt"].cancel()

    bt1 = PdfBatchTranslator(
        src, out, work, translator=make_translator(engine="mock"), max_workers=1, on_progress=on_progress
    )
    holder["bt"] = bt1
    ckpt1 = bt1.run()
    assert ckpt1.status == "cancelled"
    completed_before = set(ckpt1.completed_pages)
    assert 0 < len(completed_before) < 16

    bt2 = PdfBatchTranslator.from_checkpoint(work, translator=make_translator(engine="mock"))
    ckpt2 = bt2.resume()
    assert ckpt2.status == "completed"
    assert ckpt2.completed_pages == list(range(16))
    assert ckpt2.pending_pages == []
    assert out.is_file()


def test_f4_resume_zero_retranslations_for_completed_batches(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "zero_retrans.pdf", num_pages=16, dense=True)
    out = tmp_path / "zero_retrans_out.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def on_progress(ckpt):
        if len(ckpt.completed_batches) >= 1:
            holder["bt"].cancel()

    bt1 = PdfBatchTranslator(
        src, out, work, translator=make_translator(engine="mock"), max_workers=1, on_progress=on_progress
    )
    holder["bt"] = bt1
    ckpt1 = bt1.run()
    completed_pages_1 = set(ckpt1.completed_pages)

    tr2 = make_translator(engine="mock")
    bt2 = PdfBatchTranslator.from_checkpoint(work, translator=tr2)
    ckpt2 = bt2.resume()
    assert ckpt2.status == "completed"

    joined_calls = "\n".join(tr2.calls)
    for pno in completed_pages_1:
        assert f"(page {pno + 1})" not in joined_calls
        assert f"On page {pno + 1}," not in joined_calls


def test_f4_cancel_early_still_produces_valid_resumable_checkpoint(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "early_cancel.pdf", num_pages=6, dense=False)
    out = tmp_path / "early_out.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def hook(call_idx: int, text: str) -> None:
        if call_idx == 1:
            holder["bt"].cancel()

    bt1 = PdfBatchTranslator(
        src, out, work, translator=make_translator(engine="mock", on_call_hook=hook), max_workers=1
    )
    holder["bt"] = bt1
    ckpt1 = bt1.run()
    assert ckpt1.status == "cancelled"
    assert (work / "checkpoint.json").is_file()

    bt2 = PdfBatchTranslator.from_checkpoint(work, translator=make_translator(engine="mock"))
    ckpt2 = bt2.resume()
    assert ckpt2.status == "completed"
    assert out.is_file()


# ---------------------------------------------------------------------------
# F5: Glossary, Ambiguous Terms, Segment Context & Layout / QA
# ---------------------------------------------------------------------------


def test_f5_compute_glossary_hash_is_canonical_sha256():
    from mathtranslate.pdf_batch import compute_glossary_hash

    h1 = compute_glossary_hash({"b ": " 2", "a": "1"})
    h2 = compute_glossary_hash({"a": "1", "b": "2"})
    h3 = compute_glossary_hash({"a": "1", "b": "3"})
    assert re.fullmatch(r"[a-f0-9]{64}", h1)
    assert h1 == h2
    assert h1 != h3


def test_f5_glossary_consistently_enforced_across_pages(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(
        tmp_path / "glossary.pdf", num_pages=3, dense=False, with_ambiguous_terms=True
    )
    out = tmp_path / "glossary_vi.pdf"
    work = tmp_path / "work"
    glossary = {"field": "trường đại số", "kernel": "hạt nhân đồng cấu"}
    tr = make_translator(engine="mock")

    ckpt = PdfBatchTranslator(src, out, work, glossary=glossary, translator=tr).run()
    assert ckpt.status == "completed"
    with pymupdf.open(str(out)) as doc:
        for pno in range(3):
            txt = re.sub(r"\s+", " ", doc[pno].get_text("text")).lower()
            assert "trường đại số" in txt
            assert "hạt nhân đồng cấu" in txt


def test_f5_inspect_pdf_extracts_ambiguous_terms_and_default_glossary(tmp_path, pdf_factory):
    from mathtranslate.pdf_batch import inspect_pdf

    src = pdf_factory.make_text_pdf(
        tmp_path / "inspect.pdf", num_pages=4, dense=False, with_ambiguous_terms=True
    )
    info = inspect_pdf(src)
    assert info["total_pages"] == 4
    assert info["counts"]["TEXT"] == 4
    assert info["scan_pages"] == []
    assert "field" in info["ambiguous_terms"]
    assert isinstance(info["default_glossary"], dict) and len(info["default_glossary"]) >= 10


def test_f5_llm_neighbor_context_passed_without_leaks(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "llm_ctx.pdf", num_pages=2, dense=True)
    out = tmp_path / "llm_ctx_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="openai")

    ckpt = PdfBatchTranslator(src, out, work, engine="openai", translator=tr).run()
    assert ckpt.status == "completed"
    assert any("[Context before - do NOT output]:" in c or "[Context after - do NOT output]:" in c for c in tr.calls)
    with pymupdf.open(str(out)) as doc:
        full_txt = "\n".join(doc[i].get_text("text") for i in range(len(doc)))
        assert "do NOT output" not in full_txt
        assert "[Translate ONLY this paragraph]" not in full_txt
        assert "XMATHX" not in full_txt
        assert "{v0}" not in full_txt


def test_f5_layout_preservation_mathtype_images_toc_and_qa(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from mathtranslate.pdf_qa import verify_translated_pdf

    src = pdf_factory.make_text_pdf(
        tmp_path / "layout.pdf",
        num_pages=2,
        dense=False,
        with_toc=True,
        with_formulas=True,
        with_image=True,
    )
    out = tmp_path / "layout_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    ckpt = PdfBatchTranslator(src, out, work, translator=tr).run()
    assert ckpt.status == "completed"

    report = verify_translated_pdf(str(src), str(out), flagged_overflow_pages=ckpt.overflow_pages)
    assert report.passed is True
    assert report.page_count_match is True
    assert report.toc_preserved is True
    assert all(p.leaked_placeholders == [] for p in report.pages)
    assert all(p.out_images >= p.orig_images for p in report.pages)


# ---------------------------------------------------------------------------
# F6: Web UI PDF Backend API & Scan-Page Choice
# ---------------------------------------------------------------------------


def test_f6_api_pdf_inspect_returns_counts_scan_and_terms(web_client, tmp_path, pdf_factory):
    from .conftest import post_pdf

    pdf_path = pdf_factory.make_scan_and_text_pdf(
        tmp_path / "mixed_scan.pdf", total_pages=3, scan_pages=(1,), with_ambiguous_terms=True
    )
    status, data = post_pdf(web_client, "/api/pdf/inspect", pdf_path, filename="mixed_scan.pdf")
    assert status == 200
    assert data["total_pages"] == 3
    assert data["scan_pages"] == [1]
    assert data["counts"]["SCAN"] == 1
    assert data["counts"]["TEXT"] == 2
    assert isinstance(data["upload_id"], str) and len(data["upload_id"]) > 0
    assert "field" in data["ambiguous_terms"]
    assert isinstance(data["default_glossary"], dict)


def test_f6_api_translate_pdf_text_completes_and_downloads(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import poll_web_job, post_pdf

    patch_offline_text_translator(engine="google")
    pdf_path = pdf_factory.make_text_pdf(tmp_path / "web_text.pdf", num_pages=2, dense=False)
    status, payload = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="web_text.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert status == 202
    job_id = payload["job_id"]
    job = poll_web_job(web_client, job_id, stop_statuses={"done", "error"})
    assert job["status"] == "done"
    assert job["qa_report"]["passed"] is True
    assert "qa_report.json" in job["files"]
    pdf_files = [name for name in job["files"] if name.endswith(".pdf") and name != "partial.pdf"]
    assert pdf_files

    with web_client.get(f"/download/{job_id}/{pdf_files[0]}") as dl:
        assert dl.status_code == 200
        with pymupdf.open(stream=dl.data, filetype="pdf") as doc:
            assert len(doc) == 2
            p0_txt = re.sub(r"\s+", " ", doc[0].get_text("text")).lower()
            assert "ma trận" in p0_txt or "định lý" in p0_txt
            assert re.search(
                r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]",
                p0_txt,
            )

    with web_client.get(f"/download/{job_id}/qa_report.json") as qa_dl:
        assert qa_dl.status_code == 200
        assert json.loads(qa_dl.data.decode("utf-8"))["passed"] is True


def test_f6_api_translate_pdf_scan_returns_202_awaiting_scan_no_ocr(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import poll_web_job, post_pdf

    tr = patch_offline_text_translator(engine="google")
    pdf_path = pdf_factory.make_scan_and_text_pdf(tmp_path / "web_scan.pdf", total_pages=3, scan_pages=(1,))
    status, payload = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="web_scan.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert status == 202
    job_id = payload["job_id"]
    job = poll_web_job(web_client, job_id, stop_statuses={"awaiting_scan", "done", "error"})
    assert job["status"] == "awaiting_scan"
    assert job["scan_pages"] == [1]
    assert tr.call_count == 0


@pytest.mark.parametrize("choice", ["keep_original", "skip", "cancel"])
def test_f6_api_scan_choice_actions(
    choice, web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import poll_web_job, post_pdf

    patch_offline_text_translator(engine="google")
    pdf_path = pdf_factory.make_scan_and_text_pdf(tmp_path / f"choice_{choice}.pdf", total_pages=3, scan_pages=(1,))
    status, payload = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename=f"choice_{choice}.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert status == 202
    job_id = payload["job_id"]
    poll_web_job(web_client, job_id, stop_statuses={"awaiting_scan"})

    with web_client.post(f"/api/jobs/{job_id}/scan-choice", json={"choice": choice}) as c_resp:
        assert c_resp.status_code in (200, 202)
    job = poll_web_job(web_client, job_id, stop_statuses={"done", "cancelled", "error"})
    if choice == "cancel":
        assert job["status"] == "cancelled"
    else:
        assert job["status"] == "done"
        out_pdfs = [n for n in job["files"] if n.endswith(".pdf") and n != "partial.pdf"]
        assert out_pdfs
        with web_client.get(f"/download/{job_id}/{out_pdfs[0]}") as dl:
            assert dl.status_code == 200
            with pymupdf.open(stream=dl.data, filetype="pdf") as doc:
                expected_pages = 3 if choice == "keep_original" else 2
                assert len(doc) == expected_pages
                p0_txt = re.sub(r"\s+", " ", doc[0].get_text("text")).lower()
                assert "ma trận" in p0_txt or "định lý" in p0_txt
                assert re.search(
                    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]",
                    p0_txt,
                )
                if choice == "keep_original":
                    assert re.sub(r"\s+", "", doc[1].get_text("text")) == ""


def test_f6_api_job_telemetry_includes_progress_eta_overflow_and_partial(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import poll_web_job, post_pdf

    patch_offline_text_translator(engine="google")
    pdf_path = pdf_factory.make_text_pdf(tmp_path / "telemetry.pdf", num_pages=6, dense=False)
    status, payload = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="telemetry.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert status == 202
    job_id = payload["job_id"]
    job = poll_web_job(web_client, job_id, stop_statuses={"done", "error"})
    assert job["status"] == "done"
    prog = job["progress"]
    for key in ("completed_pages", "total_pages", "percent", "current_batch", "total_batches", "pages_per_sec", "eta_seconds"):
        assert key in prog
    assert prog["completed_pages"] == 6
    assert prog["total_pages"] == 6
    assert "scan_pages" in job
    assert "overflow_pages" in job
    assert "partial_available" in job


def test_f6_api_cancel_and_resume_endpoints(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import make_cancel_barrier, poll_web_job, post_pdf

    hook, entered_gate, release_gate = make_cancel_barrier(r"\(page 1[1-6]\)")
    patch_offline_text_translator(engine="google", on_call_hook=hook)
    pdf_path = pdf_factory.make_text_pdf(tmp_path / "web_cr.pdf", num_pages=16, dense=True)
    status, payload = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="web_cr.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert status == 202
    job_id = payload["job_id"]

    try:
        assert entered_gate.wait(timeout=15.0), "Later batch never entered cancel barrier"
        deadline = time.time() + 10.0
        snap = None
        while time.time() < deadline:
            with web_client.get(f"/api/jobs/{job_id}") as r:
                assert r.status_code == 200
                snap = r.get_json()
            if snap.get("partial_available"):
                break
            time.sleep(0.05)
        assert snap is not None and snap.get("partial_available") is True

        with web_client.post(f"/api/jobs/{job_id}/cancel") as cancel_resp:
            assert cancel_resp.status_code in (200, 202)
    finally:
        release_gate.set()

    cancelled_job = poll_web_job(web_client, job_id, stop_statuses={"cancelled", "done", "error"})
    assert cancelled_job["status"] == "cancelled"
    assert cancelled_job["partial_available"] is True
    assert 0 < cancelled_job["progress"]["completed_pages"] < 16

    with web_client.post(f"/api/jobs/{job_id}/resume") as res_resp:
        assert res_resp.status_code in (200, 202)
    done_job = poll_web_job(web_client, job_id, stop_statuses={"done", "error"})
    assert done_job["status"] == "done"
    assert done_job["progress"]["completed_pages"] == 16


# ---------------------------------------------------------------------------
# F7: Web UI Frontend & Editorial Design Compliance
# ---------------------------------------------------------------------------


def test_f7_index_has_pdf_tab_and_single_form(web_client):
    with web_client.get("/") as resp:
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)

    assert 'data-mode="arxiv"' in html
    assert 'data-mode="tex"' in html
    assert 'data-mode="zip"' in html
    assert 'data-mode="pdf"' in html
    assert "PDF Document" in html
    assert html.lower().count("<form") == 1
    assert html.lower().count("</form>") == 1


def test_f7_design_tokens_and_forbidden_patterns_absent(web_client):
    with web_client.get("/") as r_html:
        html = r_html.get_data(as_text=True)
    with web_client.get("/static/css/style.css") as r_css:
        css = r_css.get_data(as_text=True)

    for forbidden in ("bg-gradient", "linear-gradient", "bootstrap"):
        assert forbidden not in html.lower()
        assert forbidden not in css.lower()

    assert not re.search(r"<(?:link|script)[^>]+https?://", html, flags=re.IGNORECASE)

    for token in ("#FAF9F5", "#FFFFFF", "#F2F0E8", "#18181B", "#1B3A5C", "#1F6E43", "#9B2C2C", "#8A5314", "#E2DFD5"):
        assert token in css


def test_f7_zero_decorative_emojis_in_web_ui(web_client):
    emoji_re = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    for url in ("/", "/static/css/style.css", "/static/js/app.js"):
        with web_client.get(url) as resp:
            body = resp.get_data(as_text=True)
        assert not emoji_re.search(body), f"Decorative emoji found in {url}"


def test_f7_pdf_controls_present_in_html_and_js(web_client):
    with web_client.get("/") as r_html:
        html = r_html.get_data(as_text=True)
    with web_client.get("/static/js/app.js") as r_js:
        js = r_js.get_data(as_text=True)
    html_l = html.lower()
    js_l = js.lower()

    for token in (
        "/api/pdf/inspect",
        "/api/translate/pdf",
        "scan-choice",
        "/cancel",
        "/resume",
        "partial.pdf",
        "awaiting_scan",
        "keep_original",
        "skip",
    ):
        assert token in js
    assert "eta" in js_l and "overflow" in js_l

    assert 'data-mode="pdf"' in html
    for token in ("glossary", "cancel", "resume", "eta", "overflow"):
        assert token in html_l


def test_f7_mobile_responsive_css_rules_for_4_tabs(web_client):
    with web_client.get("/static/css/style.css") as r_css:
        css = r_css.get_data(as_text=True)
    m_tabs = re.search(r"\.mode-tabs\s*\{([^}]*)\}", css)
    assert m_tabs is not None
    tabs_block = m_tabs.group(1)
    assert "repeat(3, 1fr)" not in tabs_block
    assert re.search(r"repeat\(\s*4\s*,\s*1fr\s*\)", tabs_block) is not None

    assert "@media (max-width: 768px)" in css
    mobile_css = css.split("@media (max-width: 768px)", 1)[1]
    assert "grid-template-columns: 1fr" in mobile_css


# ---------------------------------------------------------------------------
# F8: Desktop GUI PDF Flow
# ---------------------------------------------------------------------------


def test_f8_gui_app_exposes_pdf_document_tab(gui_app):
    tab_titles = [gui_app.notebook.tab(t, "text") for t in gui_app.notebook.tabs()]
    assert "PDF Document (EN → VI)" in tab_titles


def test_f8_pdf_controller_inspect_and_translate_text_pdf(
    tmp_path, pdf_factory, patch_offline_text_translator
):
    from gui.controller import PdfTranslationController

    patch_offline_text_translator(engine="google")
    src = pdf_factory.make_text_pdf(tmp_path / "gui_text.pdf", num_pages=2, dense=False)
    out = tmp_path / "gui_text_vi.pdf"
    work = tmp_path / "gui_work"

    ctrl = PdfTranslationController()
    inspect_fn = getattr(ctrl, "inspect", None) or getattr(ctrl, "inspect_pdf", None)
    assert inspect_fn is not None, "PdfTranslationController must expose inspect/inspect_pdf"
    info = inspect_fn(str(src))
    assert info["total_pages"] == 2
    assert info["scan_pages"] == []

    ctrl.start(str(src), str(out), work_dir=str(work), engine="google", l_from="en", l_to="vi")
    ctrl.wait(timeout=15)
    assert out.is_file()
    assert (work / "qa_report.json").is_file()


def test_f8_pdf_controller_scan_alert_and_choice_no_auto_ocr(
    tmp_path, pdf_factory, patch_offline_text_translator
):
    from gui.controller import PdfTranslationController
    from mathtranslate.pdf_doc import ScanPagesDetectedError

    tr = patch_offline_text_translator(engine="google")
    src = pdf_factory.make_scan_and_text_pdf(tmp_path / "gui_scan.pdf", total_pages=3, scan_pages=(1,))
    out_abort = tmp_path / "gui_scan_abort_vi.pdf"
    out_skip = tmp_path / "gui_scan_skip_vi.pdf"
    out_keep = tmp_path / "gui_scan_keep_vi.pdf"

    ctrl = PdfTranslationController()
    inspect_fn = getattr(ctrl, "inspect", None) or getattr(ctrl, "inspect_pdf", None)
    assert inspect_fn is not None
    info = inspect_fn(str(src))
    assert info["scan_pages"] == [1]
    assert tr.call_count == 0

    sig = inspect.signature(ctrl.start)
    if "scan_policy" not in sig.parameters:
        pytest.fail("PdfTranslationController.start must accept scan_policy")

    # 1. Unconfirmed / abort policy must reject scan pages cleanly without calling translator or writing output
    aborted_sync = False
    try:
        ctrl.start(str(src), str(out_abort), work_dir=str(tmp_path / "work_abort"), engine="google", scan_policy="abort")
    except ScanPagesDetectedError as exc:
        aborted_sync = True
        assert exc.scan_pages == [1]
    if not aborted_sync:
        ctrl.wait(timeout=15)
        statuses = []
        while not ctrl.status_queue.empty():
            statuses.append(ctrl.status_queue.get_nowait())
        assert any(not ok for ok, *_ in statuses), "Abort scan policy must signal failure/scan alert"
    assert tr.call_count == 0
    assert not out_abort.is_file()

    # 2. Skip scan pages -> translates only the 2 text pages
    ctrl.start(str(src), str(out_skip), work_dir=str(tmp_path / "work_skip"), engine="google", scan_policy="skip")
    ctrl.wait(timeout=15)
    assert out_skip.is_file()
    with pymupdf.open(str(out_skip)) as doc:
        assert len(doc) == 2

    # 3. Keep original scan page -> preserves all 3 pages and leaves scan page untouched
    ctrl.start(str(src), str(out_keep), work_dir=str(tmp_path / "work_keep"), engine="google", scan_policy="keep_original")
    ctrl.wait(timeout=15)
    assert out_keep.is_file()
    with pymupdf.open(str(out_keep)) as doc:
        assert len(doc) == 3
        assert re.sub(r"\s+", "", doc[1].get_text("text")) == ""


def test_f8_pdf_controller_cancel_and_resume(tmp_path, pdf_factory, patch_offline_text_translator):
    from gui.controller import PdfTranslationController
    from .conftest import make_cancel_barrier

    hook, entered_gate, release_gate = make_cancel_barrier(r"\(page 1[1-6]\)")
    patch_offline_text_translator(engine="google", on_call_hook=hook)
    src = pdf_factory.make_text_pdf(tmp_path / "gui_cr.pdf", num_pages=16, dense=True)
    out = tmp_path / "gui_cr_vi.pdf"
    work = tmp_path / "gui_work"

    ctrl = PdfTranslationController()
    ctrl.start(str(src), str(out), work_dir=str(work), engine="google", l_from="en", l_to="vi")
    try:
        assert entered_gate.wait(timeout=15.0), "Controller worker never entered cancel barrier"
        ckpt_file = work / "checkpoint.json"
        partial_file = work / "partial.pdf"
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if ckpt_file.is_file() and partial_file.is_file():
                snap = json.loads(ckpt_file.read_text(encoding="utf-8"))
                if len(snap.get("completed_batches", [])) >= 1:
                    break
            time.sleep(0.05)
        assert ckpt_file.is_file() and partial_file.is_file()
        ctrl.cancel()
    finally:
        release_gate.set()

    ctrl.wait(timeout=15)
    saved = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
    assert saved["status"] == "cancelled"
    assert len(saved["pending_pages"]) > 0
    with pymupdf.open(str(work / "partial.pdf")) as pdoc:
        assert 1 <= len(pdoc) < 16

    ctrl.resume()
    ctrl.wait(timeout=15)
    saved2 = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
    assert saved2["status"] == "completed"
    assert out.is_file()


def test_f8_gui_app_pdf_tab_exposes_glossary_progress_and_cancel_controls(gui_app):
    tab_ids = gui_app.notebook.tabs()
    pdf_tab_id = next(t for t in tab_ids if gui_app.notebook.tab(t, "text") == "PDF Document (EN → VI)")
    pdf_frame = gui_app.nametowidget(pdf_tab_id)

    texts = " | ".join(_collect_tk_texts(pdf_frame)).lower()
    widgets = _collect_tk_widgets(pdf_frame)

    assert "pdf" in texts
    assert any(k in texts for k in ("glossary", "thuật ngữ"))
    assert any(k in texts for k in ("hủy", "cancel"))
    assert any(k in texts for k in ("tiếp tục", "resume"))
    assert any(k in texts for k in ("eta", "tiến độ", "progress", "tràn", "overflow"))
    assert len(widgets) >= 5
