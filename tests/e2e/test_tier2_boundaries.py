"""Tier 2 E2E Boundary, Corner-Case & Negative Tests: >= 5 tests per F1-F8 (>= 40 total)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import threading
import time

import pymupdf
import pytest
import requests


# ---------------------------------------------------------------------------
# F1 Boundaries: Empty/1p PDFs, [5,10] clamping, Retry Exhaustion, Fast-Fail
# ---------------------------------------------------------------------------


def test_f1_b_empty_zero_char_pdf_completes_cleanly(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_empty_pdf(tmp_path / "empty.pdf", num_pages=1)
    out = tmp_path / "empty_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    ckpt = PdfBatchTranslator(src, out, work, translator=tr).run()
    assert ckpt.status == "completed"
    assert ckpt.total_pages == 1
    assert tr.call_count == 0
    with pymupdf.open(str(out)) as doc:
        assert len(doc) == 1


def test_f1_b_single_page_pdf_produces_single_batch(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "one.pdf", num_pages=1, dense=False)
    out = tmp_path / "one_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    ckpt = PdfBatchTranslator(src, out, work, translator=tr).run()
    assert ckpt.status == "completed"
    assert ckpt.batches == [[0]]
    assert ckpt.completed_pages == [0]


@pytest.mark.parametrize("dense", [False, True])
def test_f1_b_batch_size_clamped_to_5_10_across_extreme_densities(
    dense, tmp_path, pdf_factory, make_translator
):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / f"clamp_{dense}.pdf", num_pages=22, dense=dense)
    out = tmp_path / f"clamp_{dense}_vi.pdf"
    work = tmp_path / f"work_{dense}"
    tr = make_translator(engine="mock")

    ckpt = PdfBatchTranslator(src, out, work, translator=tr).run()
    assert ckpt.status == "completed"
    assert all(1 <= len(b) <= 10 for b in ckpt.batches)
    assert all(len(b) >= 5 for b in ckpt.batches[:-1])
    assert sorted(p for b in ckpt.batches for p in b) == list(range(22))


def test_f1_b_retry_exhaustion_on_persistent_429_does_not_hang(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from .conftest import make_http_error

    src = pdf_factory.make_text_pdf(tmp_path / "exhaust.pdf", num_pages=1, dense=False)
    out = tmp_path / "exhaust_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock", always_fail=make_http_error(429))

    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=1, max_retries=2, base_backoff=0.005)
    try:
        ckpt = bt.run()
        assert ckpt.status in {"failed", "error"} or bool(ckpt.error_pages)
    except requests.HTTPError as exc:
        assert exc.response is not None and exc.response.status_code == 429
    assert tr.attempt_count >= 3
    saved = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
    assert saved["status"] in {"failed", "error"} or bool(saved["error_pages"])


def test_f1_b_non_retryable_http_401_fails_fast(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from .conftest import make_http_error

    src = pdf_factory.make_text_pdf(tmp_path / "auth401.pdf", num_pages=1, dense=False)
    out = tmp_path / "auth401_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock", always_fail=make_http_error(401, "Unauthorized"))

    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=1, max_retries=5, base_backoff=0.02)
    t0 = time.monotonic()
    try:
        ckpt = bt.run()
        assert ckpt.status in {"failed", "error"} or bool(ckpt.error_pages)
    except requests.HTTPError as exc:
        assert exc.response is not None and exc.response.status_code == 401
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5
    assert 1 <= tr.attempt_count <= 2
    saved = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
    assert saved["status"] in {"failed", "error"} or bool(saved["error_pages"])


def test_f1_b_single_worker_max_workers_1_no_deadlock(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "w1.pdf", num_pages=6, dense=False)
    out = tmp_path / "w1_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock")

    ckpt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=1).run()
    assert ckpt.status == "completed"
    assert tr.max_active_calls == 1


# ---------------------------------------------------------------------------
# F2 Boundaries: Empty/Unicode/SQL-Metachar TM, 100% Cache Hit, Invalidation
# ---------------------------------------------------------------------------


def test_f2_b_empty_and_whitespace_strings_in_tm(tmp_path):
    from mathtranslate.pdf_batch import SQLiteTranslationMemory

    tm = SQLiteTranslationMemory(tmp_path / "tm.sqlite3")
    try:
        tm.store("", "", "gh", "google", "en", "vi")
        tm.store("   \n\t ", "   ", "gh", "google", "en", "vi")
        assert tm.lookup("", "gh", "google", "en", "vi") == ""
        assert tm.lookup("   \n\t ", "gh", "google", "en", "vi") == "   "
    finally:
        tm.close()


def test_f2_b_unicode_and_sql_metacharacters_in_tm(tmp_path):
    from mathtranslate.pdf_batch import SQLiteTranslationMemory

    tm = SQLiteTranslationMemory(tmp_path / "tm.sqlite3")
    weird_src = "'; DROP TABLE translations; -- \"quoted\" ∫ f(x) dx = α + β\nline2"
    weird_dst = "Bản dịch tiếng Việt: '; DROP TABLE -- \"trích dẫn\" ∫ f(x) dx\nđầy đủ dấu"
    try:
        tm.store(weird_src, weird_dst, "gh_vi", "openai", "en", "vi")
        assert tm.lookup(weird_src, "gh_vi", "openai", "en", "vi") == weird_dst
        assert tm.count() == 1
    finally:
        tm.close()


def test_f2_b_large_glossary_order_and_whitespace_invariant():
    from mathtranslate.pdf_batch import compute_glossary_hash

    g1 = {f"term_{i}": f"thuật ngữ {i}" for i in range(25)}
    g2 = {f"  term_{i}  ": f" thuật ngữ {i} " for i in reversed(range(25))}
    assert compute_glossary_hash(g1) == compute_glossary_hash(g2)


def test_f2_b_100_percent_cache_hit_never_invokes_failing_translator(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "cached.pdf", num_pages=2, dense=False)
    work = tmp_path / "work"

    PdfBatchTranslator(src, tmp_path / "out1.pdf", work, translator=make_translator(engine="mock")).run()

    failing_tr = make_translator(engine="mock", always_fail=RuntimeError("Offline failure!"))
    ckpt2 = PdfBatchTranslator(src, tmp_path / "out2.pdf", work, translator=failing_tr).run()
    assert ckpt2.status == "completed"
    assert failing_tr.attempt_count == 0


def test_f2_b_single_char_glossary_value_change_invalidates_tm(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "inv.pdf", num_pages=2, dense=False, with_ambiguous_terms=True)
    work = tmp_path / "work"

    tr1 = make_translator(engine="mock")
    PdfBatchTranslator(src, tmp_path / "out1.pdf", work, glossary={"field": "trường"}, translator=tr1).run()
    assert tr1.call_count > 0

    tr2 = make_translator(engine="mock")
    PdfBatchTranslator(src, tmp_path / "out2.pdf", work, glossary={"field": "miền"}, translator=tr2).run()
    assert tr2.call_count > 0


# ---------------------------------------------------------------------------
# F3 Boundaries: Corrupt/Missing Checkpoint, No Leftover .tmp, Error Pages
# ---------------------------------------------------------------------------


def test_f3_b_corrupt_truncated_checkpoint_json_raises_error(tmp_path):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    work = tmp_path / "corrupt_work"
    work.mkdir(parents=True)
    (work / "checkpoint.json").write_text('{"job_id": "broken"', encoding="utf-8")

    with pytest.raises((ValueError, json.JSONDecodeError)):
        PdfBatchTranslator.from_checkpoint(work)


def test_f3_b_missing_checkpoint_json_raises_file_not_found(tmp_path):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    empty_work = tmp_path / "empty_work"
    empty_work.mkdir(parents=True)

    with pytest.raises((FileNotFoundError, ValueError)):
        PdfBatchTranslator.from_checkpoint(empty_work)


def test_f3_b_no_leftover_tmp_files_after_atomic_writes(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "atomic.pdf", num_pages=6, dense=False)
    out = tmp_path / "atomic_vi.pdf"
    work = tmp_path / "work"

    PdfBatchTranslator(src, out, work, translator=make_translator(engine="mock")).run()
    leftover_tmp = [p.name for p in work.iterdir() if ".tmp" in p.name] + [
        p.name for p in out.parent.iterdir() if ".tmp" in p.name
    ]
    assert leftover_tmp == []


def test_f3_b_partial_pdf_after_single_batch_is_immediately_openable(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "p1.pdf", num_pages=16, dense=True)
    out = tmp_path / "p1_vi.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def on_progress(ckpt):
        if len(ckpt.completed_batches) == 1:
            holder["bt"].cancel()

    bt = PdfBatchTranslator(
        src, out, work, translator=make_translator(engine="mock"), max_workers=1, on_progress=on_progress
    )
    holder["bt"] = bt
    ckpt = bt.run()
    assert ckpt.status == "cancelled"

    with pymupdf.open(str(work / "partial.pdf")) as pdoc:
        assert len(pdoc) >= 1


def test_f3_b_error_pages_populated_when_page_permanently_fails(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "err_page.pdf", num_pages=2, dense=False)
    out = tmp_path / "err_page_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock", always_fail=ValueError("Permanent error"))

    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=1, max_retries=1, base_backoff=0.005)
    try:
        ckpt = bt.run()
        assert bool(ckpt.error_pages) or ckpt.status == "failed"
    except ValueError:
        pass
    assert (work / "checkpoint.json").is_file()
    saved = json.loads((work / "checkpoint.json").read_text(encoding="utf-8"))
    assert saved["status"] in {"failed", "error"} or len(saved["error_pages"]) > 0


# ---------------------------------------------------------------------------
# F4 Boundaries: Double Cancel, Completed No-Op Resume, Cancel During Backoff
# ---------------------------------------------------------------------------


def test_f4_b_double_cancel_is_idempotent(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "dbl.pdf", num_pages=12, dense=True)
    out = tmp_path / "dbl_vi.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def on_progress(ckpt):
        holder["bt"].cancel()
        holder["bt"].cancel()

    bt = PdfBatchTranslator(
        src, out, work, translator=make_translator(engine="mock"), max_workers=1, on_progress=on_progress
    )
    holder["bt"] = bt
    ckpt = bt.run()
    bt.cancel()
    assert ckpt.status == "cancelled"


def test_f4_b_resume_on_already_completed_checkpoint_is_noop(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "done_res.pdf", num_pages=3, dense=False)
    out = tmp_path / "done_res_vi.pdf"
    work = tmp_path / "work"

    PdfBatchTranslator(src, out, work, translator=make_translator(engine="mock")).run()
    tr2 = make_translator(engine="mock")
    ckpt2 = PdfBatchTranslator.from_checkpoint(work, translator=tr2).resume()
    assert ckpt2.status == "completed"
    assert tr2.call_count == 0


def test_f4_b_from_checkpoint_with_max_workers_override(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "mw.pdf", num_pages=14, dense=True)
    out = tmp_path / "mw_vi.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def on_progress(ckpt):
        if len(ckpt.completed_batches) >= 1:
            holder["bt"].cancel()

    bt1 = PdfBatchTranslator(
        src, out, work, translator=make_translator(engine="mock"), max_workers=1, on_progress=on_progress
    )
    holder["bt"] = bt1
    bt1.run()

    tr2 = make_translator(engine="mock", per_call_delay=0.01)
    bt2 = PdfBatchTranslator.from_checkpoint(work, translator=tr2, max_workers=2)
    ckpt2 = bt2.resume()
    assert ckpt2.status == "completed"


def test_f4_b_cancel_during_retry_backoff_exits_promptly(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "backoff_cancel.pdf", num_pages=2, dense=False)
    out = tmp_path / "bc_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(engine="mock", fail_first_n=10)
    bt = PdfBatchTranslator(src, out, work, translator=tr, max_workers=1, max_retries=6, base_backoff=0.3)

    t = threading.Timer(0.05, bt.cancel)
    t.start()
    t0 = time.monotonic()
    try:
        ckpt = bt.run()
        assert ckpt.status == "cancelled"
    finally:
        t.cancel()
        t.join(timeout=2)
    assert time.monotonic() - t0 < 2.5


def test_f4_b_resume_works_even_if_partial_pdf_deleted(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(tmp_path / "del_partial.pdf", num_pages=14, dense=True)
    out = tmp_path / "del_partial_vi.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def on_progress(ckpt):
        if len(ckpt.completed_batches) >= 1:
            holder["bt"].cancel()

    bt1 = PdfBatchTranslator(
        src, out, work, translator=make_translator(engine="mock"), max_workers=1, on_progress=on_progress
    )
    holder["bt"] = bt1
    bt1.run()
    (work / "partial.pdf").unlink(missing_ok=True)

    ckpt2 = PdfBatchTranslator.from_checkpoint(work, translator=make_translator(engine="mock")).resume()
    assert ckpt2.status == "completed"
    assert out.is_file()


# ---------------------------------------------------------------------------
# F5 Boundaries: Classification Thresholds, >8 Glossary Terms, QA Failures
# ---------------------------------------------------------------------------


def test_f5_b_classify_scan_vs_mixed_vs_text_boundaries(tmp_path, pdf_factory):
    from mathtranslate.pdf_doc import classify_pdf_pages

    scan_p = pdf_factory.make_scan_pdf(tmp_path / "scan.pdf", num_pages=1)
    mixed_p = pdf_factory.make_mixed_pdf(tmp_path / "mixed.pdf", num_pages=1)
    text_p = pdf_factory.make_text_pdf(tmp_path / "text.pdf", num_pages=1, with_image=True)
    empty_p = pdf_factory.make_empty_pdf(tmp_path / "empty.pdf", num_pages=1)

    with pymupdf.open(str(scan_p)) as d:
        assert classify_pdf_pages(d)[0].kind == "SCAN"
    with pymupdf.open(str(mixed_p)) as d:
        assert classify_pdf_pages(d)[0].kind == "MIXED"
    with pymupdf.open(str(text_p)) as d:
        assert classify_pdf_pages(d)[0].kind == "TEXT"
    with pymupdf.open(str(empty_p)) as d:
        assert classify_pdf_pages(d)[0].kind == "TEXT"


def test_f5_b_empty_and_none_glossary_hashes_match():
    from mathtranslate.pdf_batch import compute_glossary_hash

    assert compute_glossary_hash(None) == compute_glossary_hash({})


def test_f5_b_many_glossary_terms_gt_8_preserve_placeholders(tmp_path, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from mathtranslate.pdf_qa import verify_translated_pdf

    src = tmp_path / "many_terms.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            pymupdf.Rect(72, 72, 540, 220),
            "Every theorem, lemma, corollary, proposition, matrix, eigenvalue, eigenvector, "
            "determinant, subspace, basis, and vector space with inline \uf0b1 works.",
            fontsize=11,
        )
        doc.save(str(src))

    out = tmp_path / "many_terms_vi.pdf"
    tr = make_translator(engine="openai")
    glossary = {
        "theorem": "định lý",
        "lemma": "bổ đề",
        "corollary": "hệ quả",
        "proposition": "mệnh đề",
        "matrix": "ma trận",
        "eigenvalue": "giá trị riêng",
        "eigenvector": "vectơ riêng",
        "determinant": "định thức",
        "subspace": "không gian con",
        "basis": "cơ sở",
        "vector space": "không gian vectơ",
    }
    ckpt = PdfBatchTranslator(src, out, tmp_path / "work", engine="openai", glossary=glossary, translator=tr).run()
    assert ckpt.status == "completed"
    qa = verify_translated_pdf(str(src), str(out))
    assert qa.passed is True
    assert qa.pages[0].leaked_placeholders == []


def test_f5_b_extract_ambiguous_terms_threshold_1_vs_2(tmp_path):
    from mathtranslate.pdf_doc import extract_ambiguous_terms

    one_hit = tmp_path / "one_hit.pdf"
    with pymupdf.open() as d:
        p = d.new_page()
        p.insert_text((72, 72), "Only one field appears here.", fontsize=11)
        d.save(str(one_hit))

    two_hits = tmp_path / "two_hits.pdf"
    with pymupdf.open() as d:
        p = d.new_page()
        p.insert_text((72, 72), "This field maps to another field.", fontsize=11)
        d.save(str(two_hits))

    with pymupdf.open(str(one_hit)) as d1:
        assert extract_ambiguous_terms(d1) == {}
    with pymupdf.open(str(two_hits)) as d2:
        assert "field" in extract_ambiguous_terms(d2)


def test_f5_b_verify_translated_pdf_detects_leaked_placeholder_and_missing_vi(tmp_path):
    from mathtranslate.pdf_qa import verify_translated_pdf

    orig = tmp_path / "orig.pdf"
    bad_out = tmp_path / "bad_out.pdf"
    with pymupdf.open() as d:
        p = d.new_page()
        p.insert_textbox(
            pymupdf.Rect(72, 72, 500, 200),
            "This mathematical theorem requires at least twelve distinct English words to check diacritics.",
            fontsize=11,
        )
        d.save(str(orig))

    with pymupdf.open() as d:
        p = d.new_page()
        p.insert_textbox(
            pymupdf.Rect(72, 72, 500, 200),
            "Unchanged English text with leaked placeholder {v0} and no Vietnamese.",
            fontsize=11,
        )
        d.save(str(bad_out))

    qa = verify_translated_pdf(str(orig), str(bad_out))
    assert qa.passed is False
    assert qa.pages[0].status == "FAIL"
    assert qa.pages[0].leaked_placeholders


def test_f5_b_scan_pages_detected_error_formats_over_25_pages():
    from mathtranslate.pdf_doc import ScanPagesDetectedError

    err = ScanPagesDetectedError(list(range(30)))
    msg = str(err)
    assert "(+5 trang khác)" in msg
    assert "KHÔNG tự ý chạy OCR" in msg


# ---------------------------------------------------------------------------
# F6 Boundaries: Bad Uploads, Invalid Scan Choices, Path Traversal, 404s
# ---------------------------------------------------------------------------


def test_f6_b_inspect_rejects_missing_or_non_pdf_file(web_client):
    with web_client.post("/api/pdf/inspect", data={}, content_type="multipart/form-data") as r_missing:
        assert r_missing.status_code == 400

    with web_client.post(
        "/api/pdf/inspect",
        data={"file": (io.BytesIO(b"not a pdf"), "notes.txt")},
        content_type="multipart/form-data",
    ) as r_txt:
        assert r_txt.status_code == 400


def test_f6_b_translate_pdf_rejects_invalid_engine_lang_and_glossary_json(
    web_client, tmp_path, pdf_factory
):
    from .conftest import post_pdf

    pdf_path = pdf_factory.make_text_pdf(tmp_path / "bad_params.pdf", num_pages=1)

    s1, _ = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="bad.pdf",
        extra_data={"engine": "bogus_engine", "l_from": "en", "l_to": "vi"},
    )
    assert s1 == 400

    s2, _ = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="bad.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "xx_invalid"},
    )
    assert s2 == 400

    s3, _ = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="bad.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi", "glossary": "{not-json"},
    )
    assert s3 == 400


def test_f6_b_scan_choice_rejects_invalid_choice_without_ocr(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import poll_web_job, post_pdf

    tr = patch_offline_text_translator(engine="google")
    pdf_path = pdf_factory.make_scan_and_text_pdf(tmp_path / "bad_choice.pdf", total_pages=2, scan_pages=(0,))
    status, payload = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="bad_choice.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert status == 202
    job_id = payload["job_id"]
    poll_web_job(web_client, job_id, stop_statuses={"awaiting_scan"})

    with web_client.post(f"/api/jobs/{job_id}/scan-choice", json={"choice": "auto_ocr"}) as r_bad:
        assert r_bad.status_code == 400
    assert tr.call_count == 0
    with web_client.get(f"/api/jobs/{job_id}") as r_job:
        assert r_job.get_json()["status"] == "awaiting_scan"


def test_f6_b_scan_choice_unknown_or_bad_job_id(web_client):
    with web_client.post("/api/jobs/not_valid!/scan-choice", json={"choice": "skip"}) as r_bad_id:
        assert r_bad_id.status_code in (400, 404)

    with web_client.post("/api/jobs/deadbeef1234/scan-choice", json={"choice": "skip"}) as r_missing:
        assert r_missing.status_code == 404


def test_f6_b_download_blocks_path_traversal_variants(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import poll_web_job, post_pdf

    patch_offline_text_translator(engine="google")
    pdf_path = pdf_factory.make_text_pdf(tmp_path / "trav.pdf", num_pages=1, dense=False)
    status, payload = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="trav.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert status == 202
    job_id = payload["job_id"]
    poll_web_job(web_client, job_id, stop_statuses={"done", "error"})

    jobs_root = tmp_path / "web_jobs"
    (jobs_root / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")

    for bad_url in (
        f"/download/{job_id}/../secret.txt",
        f"/download/{job_id}/..%2Fsecret.txt",
        f"/download/{job_id}/..%5Csecret.txt",
        f"/download/{job_id}/subdir/partial.pdf",
    ):
        with web_client.get(bad_url) as resp:
            assert resp.status_code in (400, 404)
            assert b"TOP_SECRET" not in resp.data


def test_f6_b_cancel_and_resume_unknown_job_return_404(web_client):
    with web_client.post("/api/jobs/deadbeef1234/cancel") as r_cancel:
        assert r_cancel.status_code == 404
    with web_client.post("/api/jobs/deadbeef1234/resume") as r_resume:
        assert r_resume.status_code == 404


# ---------------------------------------------------------------------------
# F7 Boundaries: CSS Typography/Spacing, 4-State Containers, JS State Handling
# ---------------------------------------------------------------------------


def test_f7_b_typography_spacing_tokens_and_no_bouncy_hover(web_client):
    with web_client.get("/static/css/style.css") as r_css:
        css = r_css.get_data(as_text=True)
    for tok in ("--font-serif", "--font-sans", "--font-mono", "--space-1", "--space-4", "--space-8"):
        assert tok in css
    assert "translatey(-3px)" not in css.lower()


def test_f7_b_app_js_handles_awaiting_scan_and_cancelled_states(web_client):
    with web_client.get("/static/js/app.js") as r_js:
        js = r_js.get_data(as_text=True)
    assert "awaiting_scan" in js
    assert "cancelled" in js


def test_f7_b_index_contains_all_4_state_containers(web_client):
    with web_client.get("/") as r_html:
        html = r_html.get_data(as_text=True)
    for state_id in ('id="state-idle"', 'id="state-running"', 'id="state-done"', 'id="state-error"'):
        assert state_id in html


def test_f7_b_no_nested_forms_in_index_html(web_client):
    with web_client.get("/") as r_html:
        html = r_html.get_data(as_text=True).lower()
    assert html.count("<form") == 1
    assert html.count("</form>") == 1


def test_f7_b_semantic_status_bg_tokens_present_in_css(web_client):
    with web_client.get("/static/css/style.css") as r_css:
        css = r_css.get_data(as_text=True)
    for tok in ("--status-success-bg", "--status-error-bg", "--status-running-bg"):
        assert tok in css


# ---------------------------------------------------------------------------
# F8 Boundaries: Desktop GUI Controller & App Error/Idle Boundaries
# ---------------------------------------------------------------------------


def test_f8_b_pdf_controller_rejects_missing_input_file(tmp_path):
    from gui.controller import PdfTranslationController

    ctrl = PdfTranslationController()
    missing = tmp_path / "does_not_exist.pdf"
    out = tmp_path / "out.pdf"
    with pytest.raises((FileNotFoundError, ValueError)):
        ctrl.start(str(missing), str(out), work_dir=str(tmp_path / "work"))


def test_f8_b_pdf_controller_rejects_concurrent_start(
    tmp_path, pdf_factory, patch_offline_text_translator
):
    from gui.controller import PdfTranslationController

    patch_offline_text_translator(engine="google", per_call_delay=0.08)
    src = pdf_factory.make_text_pdf(tmp_path / "conc_gui.pdf", num_pages=6, dense=False)
    out = tmp_path / "conc_gui_vi.pdf"

    ctrl = PdfTranslationController()
    ctrl.start(str(src), str(out), work_dir=str(tmp_path / "w1"), engine="google")
    try:
        with pytest.raises(RuntimeError):
            ctrl.start(str(src), str(tmp_path / "out2.pdf"), work_dir=str(tmp_path / "w2"), engine="google")
    finally:
        ctrl.cancel()
        ctrl.wait(timeout=10)


def test_f8_b_pdf_controller_cancel_when_idle_is_safe():
    from gui.controller import PdfTranslationController

    ctrl = PdfTranslationController()
    ctrl.cancel()
    assert ctrl.is_running is False


def test_f8_b_gui_preferences_isolated_persistence(isolated_gui_prefs):
    from gui.controller import GuiPreferences

    prefs = GuiPreferences.load()
    prefs.engine = "openai"
    prefs.l_from = "en"
    prefs.l_to = "vi"
    prefs.save()

    reloaded = GuiPreferences.load()
    assert reloaded.engine == "openai"
    assert reloaded.l_to == "vi"
    assert any(isolated_gui_prefs.iterdir())


def test_f8_b_gui_app_handles_empty_pdf_path_gracefully(gui_app):
    # Switching to PDF tab and triggering queue poll / empty state must not raise TclError
    tab_ids = gui_app.notebook.tabs()
    pdf_tab_id = next(t for t in tab_ids if gui_app.notebook.tab(t, "text") == "PDF Document (EN → VI)")
    gui_app.notebook.select(pdf_tab_id)
    gui_app._poll_queues()
    gui_app.update_idletasks()
