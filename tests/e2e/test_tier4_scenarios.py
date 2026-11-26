"""Tier 4 E2E Real-World Application Scenarios (>= 6 scenarios)."""

from __future__ import annotations

import json
from pathlib import Path
import time

import pymupdf
import pytest


def test_t4_scenario_paper_pdf_full_5_pages_qa_and_side_by_side_png(
    tmp_path, make_translator
):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from mathtranslate.pdf_qa import verify_translated_pdf
    from .conftest import REF_PAPER_PDF

    assert REF_PAPER_PDF.is_file(), f"Missing reference paper: {REF_PAPER_PDF}"
    out_pdf = tmp_path / "paper_vi.pdf"
    work_dir = tmp_path / "paper_work"
    render_dir = tmp_path / "paper_renders"
    tr = make_translator(engine="mock")

    ckpt = PdfBatchTranslator(
        REF_PAPER_PDF,
        out_pdf,
        work_dir,
        glossary={"field": "trường"},
        translator=tr,
        max_workers=2,
    ).run()

    assert ckpt.status == "completed"
    assert ckpt.total_pages == 5
    assert ckpt.completed_pages == [0, 1, 2, 3, 4]
    assert out_pdf.is_file()

    qa = verify_translated_pdf(
        str(REF_PAPER_PDF),
        str(out_pdf),
        flagged_overflow_pages=ckpt.overflow_pages,
        render_dir=str(render_dir),
    )
    assert qa.passed is True
    assert qa.orig_pages == 5
    assert qa.out_pages == 5
    assert qa.page_count_match is True
    assert len(qa.rendered_comparisons) == 5
    for p_rep, png in zip(qa.pages, qa.rendered_comparisons):
        assert p_rep.leaked_placeholders == []
        assert p_rep.out_images >= p_rep.orig_images
        assert Path(png).is_file() and Path(png).stat().st_size > 1000


def test_t4_scenario_linear_pdf_386p_two_batches_cancel_restart_resume(
    tmp_path, make_translator
):
    from mathtranslate.pdf_batch import PdfBatchTranslator, SQLiteTranslationMemory
    from mathtranslate.pdf_qa import verify_translated_pdf
    from .conftest import LINEAR_PDF

    assert LINEAR_PDF.is_file(), f"Missing linear.pdf: {LINEAR_PDF}"
    out_pdf = tmp_path / "linear_vi.pdf"
    work_dir = tmp_path / "linear_work"

    holder: dict[str, PdfBatchTranslator] = {}

    def stop_after_two_batches(ckpt):
        if len(ckpt.completed_batches) >= 2:
            holder["bt"].cancel()

    tr1 = make_translator(engine="mock")
    bt1 = PdfBatchTranslator(
        LINEAR_PDF,
        out_pdf,
        work_dir,
        glossary={"kernel": "hạt nhân", "rank": "hạng"},
        translator=tr1,
        max_workers=1,
        on_progress=stop_after_two_batches,
    )
    holder["bt"] = bt1
    ckpt1 = bt1.run()

    assert ckpt1.status == "cancelled"
    assert ckpt1.total_pages == 386
    assert ckpt1.completed_batches == [0, 1]
    batches_1_2_pages = set(ckpt1.completed_pages)
    assert batches_1_2_pages == set(ckpt1.batches[0] + ckpt1.batches[1])

    # partial.pdf must be immediately readable with pymupdf.open()
    partial_path = work_dir / "partial.pdf"
    assert partial_path.is_file()
    with pymupdf.open(str(partial_path)) as pdoc:
        assert len(pdoc) == len(batches_1_2_pages)

    saved_ckpt = json.loads((work_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert saved_ckpt["status"] == "cancelled"
    assert saved_ckpt["total_pages"] == 386
    assert len(saved_ckpt["pending_pages"]) == 386 - len(batches_1_2_pages)

    # Simulate process restart: reload via from_checkpoint and resume from batch 3
    tr2 = make_translator(engine="mock")
    seen_batches_on_resume: list[int] = []
    batch3_idx = len(ckpt1.completed_batches)
    holder2: dict[str, PdfBatchTranslator] = {}

    def stop_after_batch3(ckpt):
        if ckpt.completed_batches:
            last_b = ckpt.completed_batches[-1]
            if last_b >= batch3_idx and last_b not in seen_batches_on_resume:
                seen_batches_on_resume.append(last_b)
                holder2["bt"].cancel()

    # Zero re-translations of batches 1-2: TM already held their entries before resume
    tm_before = SQLiteTranslationMemory(work_dir / "pdf_tm.sqlite3")
    try:
        tm_count_before = tm_before.count()
        assert tm_count_before > 0
    finally:
        tm_before.close()

    bt2 = PdfBatchTranslator.from_checkpoint(
        work_dir, translator=tr2, max_workers=1, on_progress=stop_after_batch3
    )
    holder2["bt"] = bt2
    ckpt2 = bt2.resume()

    assert ckpt2.status == "cancelled"
    assert seen_batches_on_resume == [batch3_idx]
    assert ckpt2.completed_batches == [0, 1, 2]
    assert set(ckpt1.completed_pages).issubset(set(ckpt2.completed_pages))

    tm = SQLiteTranslationMemory(work_dir / "pdf_tm.sqlite3")
    try:
        assert tm.count() > tm_count_before
    finally:
        tm.close()
    assert tr2.call_count > 0  # Batch 3 translated
    assert set(tr1.calls).isdisjoint(set(tr2.calls))  # Zero re-translation of batches 1-2

    # Check updated partial.pdf readability, remapped TOC preservation, and per-page QA
    with pymupdf.open(str(LINEAR_PDF)) as orig_doc:
        expected_toc = [
            e for e in orig_doc.get_toc(simple=False) if 1 <= e[2] <= len(ckpt2.completed_pages)
        ]
    with pymupdf.open(str(partial_path)) as pdoc:
        assert len(pdoc) == len(ckpt2.completed_pages)
        assert len(pdoc.get_toc()) == len(expected_toc) == 10
        checked = sorted(ckpt2.completed_pages)[:5]
        qa = verify_translated_pdf(
            str(LINEAR_PDF),
            str(partial_path),
            checked_pages=checked,
            flagged_overflow_pages=ckpt2.overflow_pages,
            require_page_count_match=False,
        )
        assert qa.passed is True
        for p_rep in qa.pages:
            assert p_rep.status != "FAIL"
            assert p_rep.formula_similarity >= 0.90
            assert p_rep.leaked_placeholders == []
            assert p_rep.out_images >= p_rep.orig_images


def test_t4_scenario_web_ui_full_pdf_workflow_and_design_compliance(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import make_cancel_barrier, poll_web_job, post_pdf

    hook, entered_gate, release_gate = make_cancel_barrier(r"\bPage (?:7|8|9|10|11|12):")
    patch_offline_text_translator(engine="google", on_call_hook=hook)

    # 1. Verify UI HTML & CSS editorial compliance
    with web_client.get("/") as r_html:
        html = r_html.get_data(as_text=True)
    with web_client.get("/static/css/style.css") as r_css:
        css = r_css.get_data(as_text=True)
    assert 'data-mode="pdf"' in html
    assert "#FAF9F5" in css and "#18181B" in css and "#1B3A5C" in css
    assert "linear-gradient" not in html.lower() and "linear-gradient" not in css.lower()
    assert "@media (max-width: 768px)" in css

    # 2. Inspect a multi-page PDF with scan page + ambiguous terms
    src = pdf_factory.make_scan_and_text_pdf(
        tmp_path / "web_e2e.pdf", total_pages=12, scan_pages=(1,), with_ambiguous_terms=True
    )
    insp_status, insp_data = post_pdf(web_client, "/api/pdf/inspect", src, filename="web_e2e.pdf")
    assert insp_status == 200
    assert insp_data["scan_pages"] == [1]
    assert "field" in insp_data["ambiguous_terms"]

    # 3. Start PDF translation -> 202 awaiting_scan
    start_status, start_data = post_pdf(
        web_client,
        "/api/translate/pdf",
        extra_data={
            "upload_id": insp_data["upload_id"],
            "engine": "google",
            "l_from": "en",
            "l_to": "vi",
            "glossary": json.dumps({"field": "trường đại số"}),
        },
    )
    assert start_status == 202
    job_id = start_data["job_id"]
    awaiting = poll_web_job(web_client, job_id, stop_statuses={"awaiting_scan"})
    assert awaiting["status"] == "awaiting_scan"

    # 4. Choose keep_original -> deterministic barrier cancel -> download partial.pdf -> resume -> done
    with web_client.post(f"/api/jobs/{job_id}/scan-choice", json={"choice": "keep_original"}) as c_resp:
        assert c_resp.status_code in (200, 202)
    try:
        assert entered_gate.wait(timeout=15.0), "Web worker never entered cancel barrier"
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

    cancelled = poll_web_job(web_client, job_id, stop_statuses={"cancelled", "done", "error"})
    assert cancelled["status"] == "cancelled"
    assert cancelled["partial_available"] is True

    with web_client.get(f"/download/{job_id}/partial.pdf") as pdl:
        assert pdl.status_code == 200
        with pymupdf.open(stream=pdl.data, filetype="pdf") as pdoc:
            assert 1 <= len(pdoc) < 12

    with web_client.post(f"/api/jobs/{job_id}/resume") as res_resp:
        assert res_resp.status_code in (200, 202)
    done = poll_web_job(web_client, job_id, stop_statuses={"done", "error"})
    assert done["status"] == "done"
    assert done["qa_report"]["passed"] is True

    out_pdfs = [n for n in done["files"] if n.endswith(".pdf") and n != "partial.pdf"]
    assert out_pdfs
    with web_client.get(f"/download/{job_id}/{out_pdfs[0]}") as dl:
        assert dl.status_code == 200
        with pymupdf.open(stream=dl.data, filetype="pdf") as doc:
            assert len(doc) == 12
    with web_client.get(f"/download/{job_id}/qa_report.json") as qdl:
        assert qdl.status_code == 200
        assert json.loads(qdl.data.decode("utf-8"))["passed"] is True


def test_t4_scenario_desktop_gui_controller_and_app_e2e(
    gui_app, tmp_path, pdf_factory, patch_offline_text_translator
):
    from gui.controller import PdfTranslationController
    from .conftest import make_cancel_barrier

    hook, entered_gate, release_gate = make_cancel_barrier(r"\bPage (?:7|8|9|10|11|12):")
    tr = patch_offline_text_translator(engine="google", on_call_hook=hook)

    # 1. Verify 5th tab in MathTranslateApp
    tab_ids = gui_app.notebook.tabs()
    pdf_tab_id = next(t for t in tab_ids if gui_app.notebook.tab(t, "text") == "PDF Document (EN → VI)")
    gui_app.notebook.select(pdf_tab_id)
    gui_app.update_idletasks()

    # 2. Drive PdfTranslationController through inspect -> scan policy -> deterministic cancel -> resume
    src = pdf_factory.make_scan_and_text_pdf(
        tmp_path / "gui_e2e.pdf", total_pages=12, scan_pages=(0,), with_ambiguous_terms=True
    )
    out = tmp_path / "gui_e2e_vi.pdf"
    work = tmp_path / "gui_e2e_work"

    ctrl = PdfTranslationController()
    inspect_fn = getattr(ctrl, "inspect", None) or getattr(ctrl, "inspect_pdf", None)
    info = inspect_fn(str(src))
    assert info["scan_pages"] == [0]
    assert tr.call_count == 0  # Never auto-OCR

    ctrl.start(
        str(src),
        str(out),
        work_dir=str(work),
        engine="google",
        l_from="en",
        l_to="vi",
        scan_policy="skip",
        glossary={"field": "trường đại số"},
    )
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
    assert (work / "partial.pdf").is_file()
    assert len(saved["pending_pages"]) > 0

    ctrl.resume()
    ctrl.wait(timeout=15)

    assert out.is_file()
    assert (work / "qa_report.json").is_file()
    with pymupdf.open(str(out)) as doc:
        assert len(doc) == 11


def test_t4_scenario_llm_engine_book_chapter_with_glossary_and_qa(
    tmp_path, pdf_factory, make_translator
):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from mathtranslate.pdf_qa import verify_translated_pdf

    src = pdf_factory.make_text_pdf(
        tmp_path / "chapter.pdf",
        num_pages=4,
        dense=True,
        with_toc=True,
        with_formulas=True,
        with_ambiguous_terms=True,
        with_image=True,
    )
    out = tmp_path / "chapter_vi.pdf"
    work = tmp_path / "work"
    render_dir = tmp_path / "renders"
    tr = make_translator(engine="gemini")

    ckpt = PdfBatchTranslator(
        src,
        out,
        work,
        engine="gemini",
        glossary={"field": "trường", "kernel": "hạt nhân", "rank": "hạng"},
        translator=tr,
        max_workers=2,
    ).run()
    assert ckpt.status == "completed"

    qa = verify_translated_pdf(
        str(src), str(out), flagged_overflow_pages=ckpt.overflow_pages, render_dir=str(render_dir)
    )
    assert qa.passed is True
    assert qa.toc_preserved is True
    assert len(qa.rendered_comparisons) == 4


def test_t4_scenario_multi_batch_rate_limit_storm_recovery(
    tmp_path, pdf_factory, make_translator
):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from mathtranslate.pdf_qa import verify_translated_pdf
    from .conftest import make_http_error

    src = pdf_factory.make_text_pdf(tmp_path / "storm.pdf", num_pages=14, dense=True)
    out = tmp_path / "storm_vi.pdf"
    work = tmp_path / "work"
    tr = make_translator(
        engine="mock",
        per_call_delay=0.005,
        fail_on_attempts={
            1: make_http_error(429),
            4: TimeoutError("storm timeout"),
            7: make_http_error(503, "Service Unavailable"),
        },
    )

    ckpt = PdfBatchTranslator(
        src, out, work, translator=tr, max_workers=2, max_retries=4, base_backoff=0.01
    ).run()
    assert ckpt.status == "completed"
    assert ckpt.completed_pages == list(range(14))

    qa = verify_translated_pdf(str(src), str(out), flagged_overflow_pages=ckpt.overflow_pages)
    assert qa.passed is True


def test_t4_scenario_web_ui_skip_scan_and_abort_workflows(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import poll_web_job, post_pdf

    patch_offline_text_translator(engine="google")
    src = pdf_factory.make_scan_and_text_pdf(tmp_path / "skip_scan.pdf", total_pages=5, scan_pages=(1, 3))

    # Workflow A: skip scan pages
    s1, p1 = post_pdf(
        web_client,
        "/api/translate/pdf",
        src,
        filename="skip_scan.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert s1 == 202
    jid1 = p1["job_id"]
    await1 = poll_web_job(web_client, jid1, stop_statuses={"awaiting_scan"})
    assert await1["scan_pages"] == [1, 3]

    with web_client.post(f"/api/jobs/{jid1}/scan-choice", json={"choice": "skip"}) as _:
        pass
    done1 = poll_web_job(web_client, jid1, stop_statuses={"done", "error"})
    assert done1["status"] == "done"
    pdfs = [n for n in done1["files"] if n.endswith(".pdf") and n != "partial.pdf"]
    with web_client.get(f"/download/{jid1}/{pdfs[0]}") as dl:
        assert dl.status_code == 200
        with pymupdf.open(stream=dl.data, filetype="pdf") as doc:
            assert len(doc) == 3

    # Workflow B: cancel on scan prompt
    s2, p2 = post_pdf(
        web_client,
        "/api/translate/pdf",
        src,
        filename="abort_scan.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert s2 == 202
    jid2 = p2["job_id"]
    poll_web_job(web_client, jid2, stop_statuses={"awaiting_scan"})
    with web_client.post(f"/api/jobs/{jid2}/scan-choice", json={"choice": "cancel"}) as _:
        pass
    done2 = poll_web_job(web_client, jid2, stop_statuses={"cancelled", "done", "error"})
    assert done2["status"] == "cancelled"
