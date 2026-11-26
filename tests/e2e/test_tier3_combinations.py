"""Tier 3 E2E Pairwise Cross-Feature Combination Tests (>= 10 tests)."""

from __future__ import annotations

import json
from pathlib import Path
import re
import time

import pymupdf
import pytest


def test_t3_glossary_change_invalidates_tm_and_retranslates(tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator

    src = pdf_factory.make_text_pdf(
        tmp_path / "gloss_tm.pdf", num_pages=2, dense=False, with_ambiguous_terms=True
    )
    work = tmp_path / "work"

    tr1 = make_translator(engine="mock")
    PdfBatchTranslator(
        src, tmp_path / "out_a.pdf", work, glossary={"field": "trường"}, translator=tr1
    ).run()
    assert tr1.call_count > 0

    # Same glossary -> 0 calls
    tr2 = make_translator(engine="mock")
    PdfBatchTranslator(
        src, tmp_path / "out_a2.pdf", work, glossary={"field": "trường"}, translator=tr2
    ).run()
    assert tr2.call_count == 0

    # Changed glossary -> cache miss -> re-translates with new term
    tr3 = make_translator(engine="mock")
    PdfBatchTranslator(
        src, tmp_path / "out_b.pdf", work, glossary={"field": "miền"}, translator=tr3
    ).run()
    assert tr3.call_count > 0
    with pymupdf.open(str(tmp_path / "out_b.pdf")) as doc:
        txt = doc[0].get_text("text").lower()
        assert "miền" in txt


@pytest.mark.parametrize("engine", ["openai", "mock"])
def test_t3_llm_vs_nmt_with_ambiguous_terms_and_formulas(
    engine, tmp_path, pdf_factory, make_translator
):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from mathtranslate.pdf_qa import verify_translated_pdf

    src = pdf_factory.make_text_pdf(
        tmp_path / f"eng_{engine}.pdf",
        num_pages=2,
        dense=True,
        with_formulas=True,
        with_ambiguous_terms=True,
    )
    out = tmp_path / f"eng_{engine}_vi.pdf"
    work = tmp_path / f"work_{engine}"
    tr = make_translator(engine=engine)
    glossary = {"field": "trường đại số", "kernel": "hạt nhân", "rank": "hạng"}

    ckpt = PdfBatchTranslator(src, out, work, engine=engine, glossary=glossary, translator=tr).run()
    assert ckpt.status == "completed"

    qa = verify_translated_pdf(str(src), str(out), flagged_overflow_pages=ckpt.overflow_pages)
    assert qa.passed is True
    with pymupdf.open(str(out)) as doc:
        full_txt = re.sub(
            r"\s+", " ", "\n".join(doc[i].get_text("text") for i in range(len(doc)))
        ).lower()
        assert "trường đại số" in full_txt
        assert "hạt nhân" in full_txt
        assert "do not output" not in full_txt
        assert "xmathx" not in full_txt


@pytest.mark.parametrize("policy", ["abort", "keep_original", "skip"])
def test_t3_scan_and_text_pdf_across_scan_policies(policy, tmp_path, pdf_factory, make_translator):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from mathtranslate.pdf_doc import ScanPagesDetectedError

    src = pdf_factory.make_scan_and_text_pdf(tmp_path / f"mix_{policy}.pdf", total_pages=4, scan_pages=(1,))
    out = tmp_path / f"mix_{policy}_vi.pdf"
    work = tmp_path / f"work_{policy}"
    tr = make_translator(engine="mock")

    if policy == "abort":
        with pytest.raises(ScanPagesDetectedError) as exc_info:
            PdfBatchTranslator(src, out, work, scan_policy=policy, translator=tr).run()
        assert exc_info.value.scan_pages == [1]
        assert tr.call_count == 0
        assert not out.exists()
        return

    ckpt = PdfBatchTranslator(src, out, work, scan_policy=policy, translator=tr).run()
    assert ckpt.status == "completed"
    assert ckpt.scan_pages == [1]
    assert out.is_file()
    with pymupdf.open(str(out)) as doc:
        if policy == "keep_original":
            assert len(doc) == 4
            assert re.sub(r"\s+", "", doc[1].get_text("text")) == ""
        else:
            assert len(doc) == 3


def test_t3_cancel_mid_run_partial_pdf_from_checkpoint_and_resume(
    tmp_path, pdf_factory, make_translator
):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from mathtranslate.pdf_qa import verify_translated_pdf

    src = pdf_factory.make_text_pdf(
        tmp_path / "cr_combo.pdf", num_pages=16, dense=True, with_formulas=True, with_toc=True
    )
    out = tmp_path / "cr_combo_vi.pdf"
    work = tmp_path / "work"
    holder: dict[str, PdfBatchTranslator] = {}

    def on_progress(ckpt):
        if len(ckpt.completed_batches) >= 1:
            holder["bt"].cancel()

    tr1 = make_translator(engine="mock")
    bt1 = PdfBatchTranslator(src, out, work, translator=tr1, max_workers=1, on_progress=on_progress)
    holder["bt"] = bt1
    ckpt1 = bt1.run()
    assert ckpt1.status == "cancelled"
    done_batch1 = set(ckpt1.completed_pages)

    with pymupdf.open(str(work / "partial.pdf")) as pdoc:
        assert len(pdoc) >= len(done_batch1)

    tr2 = make_translator(engine="mock")
    bt2 = PdfBatchTranslator.from_checkpoint(work, translator=tr2)
    ckpt2 = bt2.resume()
    assert ckpt2.status == "completed"
    # Zero re-translation of batch 1 pages
    for pno in done_batch1:
        assert f"On page {pno + 1}," not in "\n".join(tr2.calls)

    qa = verify_translated_pdf(str(src), str(out), flagged_overflow_pages=ckpt2.overflow_pages)
    assert qa.passed is True
    assert qa.toc_preserved is True


def test_t3_bounded_concurrency_and_429_retries_with_tm_persistence(
    tmp_path, pdf_factory, make_translator
):
    from mathtranslate.pdf_batch import PdfBatchTranslator
    from .conftest import make_http_error

    src = pdf_factory.make_text_pdf(tmp_path / "conc_retry.pdf", num_pages=12, dense=True)
    out1 = tmp_path / "cr1.pdf"
    out2 = tmp_path / "cr2.pdf"
    work = tmp_path / "work"

    tr1 = make_translator(
        engine="mock",
        per_call_delay=0.01,
        fail_on_attempts={1: make_http_error(429), 3: TimeoutError("transient")},
    )
    ckpt1 = PdfBatchTranslator(
        src, out1, work, translator=tr1, max_workers=2, max_retries=4, base_backoff=0.01
    ).run()
    assert ckpt1.status == "completed"
    assert 1 <= tr1.max_active_calls <= 2

    tr2 = make_translator(engine="mock")
    ckpt2 = PdfBatchTranslator(src, out2, work, translator=tr2, max_workers=2).run()
    assert ckpt2.status == "completed"
    assert tr2.call_count == 0


@pytest.mark.parametrize("choice", ["keep_original", "skip", "cancel"])
def test_t3_web_inspect_upload_id_and_scan_choice(
    choice, web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import poll_web_job, post_pdf

    patch_offline_text_translator(engine="google")
    pdf_path = pdf_factory.make_scan_and_text_pdf(tmp_path / f"wcombo_{choice}.pdf", total_pages=3, scan_pages=(1,))
    insp_status, insp_data = post_pdf(
        web_client, "/api/pdf/inspect", pdf_path, filename=f"wcombo_{choice}.pdf"
    )
    assert insp_status == 200
    upload_id = insp_data["upload_id"]

    start_status, start_data = post_pdf(
        web_client,
        "/api/translate/pdf",
        extra_data={
            "upload_id": upload_id,
            "engine": "google",
            "l_from": "en",
            "l_to": "vi",
            "glossary": json.dumps({"field": "trường đại số"}),
        },
    )
    assert start_status == 202
    job_id = start_data["job_id"]
    poll_web_job(web_client, job_id, stop_statuses={"awaiting_scan"})

    with web_client.post(f"/api/jobs/{job_id}/scan-choice", json={"choice": choice}) as c_resp:
        assert c_resp.status_code in (200, 202)
    job = poll_web_job(web_client, job_id, stop_statuses={"done", "cancelled", "error"})
    if choice == "cancel":
        assert job["status"] == "cancelled"
    else:
        assert job["status"] == "done"
        out_pdfs = [n for n in job["files"] if n.endswith(".pdf") and n != "partial.pdf"]
        with web_client.get(f"/download/{job_id}/{out_pdfs[0]}") as dl:
            assert dl.status_code == 200
            with pymupdf.open(stream=dl.data, filetype="pdf") as doc:
                assert len(doc) == (3 if choice == "keep_original" else 2)
                assert "trường đại số" in re.sub(r"\s+", " ", doc[0].get_text("text")).lower()


def test_t3_web_cancel_partial_download_resume_and_qa_report(
    web_client, tmp_path, pdf_factory, patch_offline_text_translator
):
    from .conftest import make_cancel_barrier, poll_web_job, post_pdf

    hook, entered_gate, release_gate = make_cancel_barrier(r"\(page 1[1-6]\)")
    patch_offline_text_translator(engine="google", on_call_hook=hook)
    pdf_path = pdf_factory.make_text_pdf(tmp_path / "web_full_cr.pdf", num_pages=16, dense=True)
    status, payload = post_pdf(
        web_client,
        "/api/translate/pdf",
        pdf_path,
        filename="web_full_cr.pdf",
        extra_data={"engine": "google", "l_from": "en", "l_to": "vi"},
    )
    assert status == 202
    job_id = payload["job_id"]

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

    job_c = poll_web_job(web_client, job_id, stop_statuses={"cancelled", "done", "error"})
    assert job_c["status"] == "cancelled"
    assert job_c["partial_available"] is True

    with web_client.get(f"/download/{job_id}/partial.pdf") as pdl:
        assert pdl.status_code == 200
        with pymupdf.open(stream=pdl.data, filetype="pdf") as pdoc:
            assert 1 <= len(pdoc) < 16

    with web_client.post(f"/api/jobs/{job_id}/resume") as r_res:
        assert r_res.status_code in (200, 202)
    job_done = poll_web_job(web_client, job_id, stop_statuses={"done", "error"})
    assert job_done["status"] == "done"

    assert job_done["qa_report"]["passed"] is True
    with web_client.get(f"/download/{job_id}/qa_report.json") as qdl:
        assert qdl.status_code == 200
        assert json.loads(qdl.data.decode("utf-8"))["passed"] is True


def test_t3_gui_controller_scan_policy_glossary_cancel_and_resume(
    tmp_path, pdf_factory, patch_offline_text_translator
):
    from gui.controller import PdfTranslationController
    from .conftest import make_cancel_barrier

    hook, entered_gate, release_gate = make_cancel_barrier(r"\bPage (?:7|8|9|10|11|12):")
    patch_offline_text_translator(engine="google", on_call_hook=hook)
    src = pdf_factory.make_scan_and_text_pdf(
        tmp_path / "gui_combo.pdf", total_pages=12, scan_pages=(1,), with_ambiguous_terms=True
    )
    out = tmp_path / "gui_combo_vi.pdf"
    work = tmp_path / "gui_work"

    ctrl = PdfTranslationController()
    inspect_fn = getattr(ctrl, "inspect", None) or getattr(ctrl, "inspect_pdf", None)
    info = inspect_fn(str(src))
    assert info["scan_pages"] == [1]

    ctrl.start(
        str(src),
        str(out),
        work_dir=str(work),
        engine="google",
        l_from="en",
        l_to="vi",
        scan_policy="keep_original",
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
    with pymupdf.open(str(out)) as doc:
        assert len(doc) == 12
        assert "trường đại số" in re.sub(r"\s+", " ", doc[0].get_text("text")).lower()
