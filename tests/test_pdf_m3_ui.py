"""Tests for PDF-M3 Web UI and Desktop GUI integration."""

from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path

import pymupdf as fitz
import pytest

from gui.controller import PdfTranslationController
from mathtranslate.engines.base import BaseTranslator
from web.app import JOBS, JOBS_LOCK, app


class _DummyPdfTranslator(BaseTranslator):
    supports_multithread = True

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[str] = []

    def translate(self, text: str, l_from: str = "en", l_to: str = "vi") -> str:
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(text)
        return f"[VI] {text}"


def _make_text_pdf(path: Path, pages: int = 3) -> Path:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Page {i + 1}: This is an English mathematics paragraph.", fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


def _make_scan_pdf(path: Path) -> Path:
    doc = fitz.open()
    page1 = doc.new_page(width=595, height=842)
    page1.insert_text((72, 100), "Page 1 has selectable text.", fontsize=12)

    page2 = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200), 0)
    pix.clear_with(240)
    page2.insert_image(fitz.Rect(50, 50, 500, 700), stream=pix.tobytes("png"))
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def web_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(app.config, "JOBS_ROOT", str(tmp_path / "web_jobs"))
    monkeypatch.setitem(app.config, "TESTING", True)
    with JOBS_LOCK:
        JOBS.clear()
    with app.test_client() as client:
        yield client
    with JOBS_LOCK:
        JOBS.clear()


def _wait_for_job(client, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        if data["status"] in {"completed", "done", "failed", "cancelled", "awaiting_scan"}:
            return data
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not settle within {timeout}s")


def test_web_ui_html_css_js_pdf_tab_invariants(web_client):
    resp = web_client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert 'data-mode="pdf"' in html
    assert "PDF Document" in html
    assert html.lower().count("<form") == 1
    assert "http://" not in html and "https://" not in html
    assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", html)
    for kw in ("glossary", "cancel", "resume", "eta", "overflow"):
        assert kw in html.lower()

    with web_client.get("/static/css/style.css") as css_resp:
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert "repeat(4, 1fr)" in css
        assert "linear-gradient" not in css

    with web_client.get("/static/js/app.js") as js_resp:
        assert js_resp.status_code == 200
        js = js_resp.get_data(as_text=True)
        assert "/api/translate/pdf" in js
        assert "/api/pdf/inspect" in js


def test_web_pdf_inspect_and_scan_choice_flow(web_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scan_pdf = _make_scan_pdf(tmp_path / "scan_sample.pdf")
    dummy = _DummyPdfTranslator()
    monkeypatch.setattr("mathtranslate.engines.create_engine", lambda *a, **kw: dummy)
    monkeypatch.setattr("mathtranslate.translate.create_engine", lambda *a, **kw: dummy)

    with open(scan_pdf, "rb") as fh:
        inspect_resp = web_client.post(
            "/api/pdf/inspect",
            data={"file": (io.BytesIO(fh.read()), "scan_sample.pdf")},
            content_type="multipart/form-data",
        )
    assert inspect_resp.status_code == 200
    info = inspect_resp.get_json()
    assert info["total_pages"] == 2
    assert info["scan_pages"] == [1]
    upload_id = info["upload_id"]

    start_resp = web_client.post(
        "/api/translate/pdf",
        data={"upload_id": upload_id, "engine": "google", "l_from": "en", "l_to": "vi", "scan_policy": "abort"},
    )
    assert start_resp.status_code == 202
    start_data = start_resp.get_json()
    assert start_data["status"] == "awaiting_scan"
    assert start_data["scan_pages"] == [1]
    assert dummy.calls == []  # Zero auto-OCR / no translation before user choice

    job_id = start_data["job_id"]
    choice_resp = web_client.post(
        f"/api/jobs/{job_id}/scan-choice",
        json={"choice": "skip"},
    )
    assert choice_resp.status_code == 200
    final_job = _wait_for_job(web_client, job_id)
    assert final_job["status"] == "done"
    assert final_job["qa_report"] is not None
    assert "qa_report.json" in final_job["files"]
    assert "scan_sample_translated.pdf" in final_job["files"]


def test_web_pdf_cancel_partial_and_resume(web_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf_path = _make_text_pdf(tmp_path / "multi_batch.pdf", pages=12)
    dummy = _DummyPdfTranslator(delay=0.08)
    monkeypatch.setattr("mathtranslate.engines.create_engine", lambda *a, **kw: dummy)
    monkeypatch.setattr("mathtranslate.translate.create_engine", lambda *a, **kw: dummy)

    with open(pdf_path, "rb") as fh:
        resp = web_client.post(
            "/api/translate/pdf",
            data={
                "file": (io.BytesIO(fh.read()), "multi_batch.pdf"),
                "engine": "google",
                "l_from": "en",
                "l_to": "vi",
                "scan_policy": "keep_original",
                "glossary": json.dumps({"mathematics": "toán học"}),
            },
            content_type="multipart/form-data",
        )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    # Wait briefly so at least one batch starts/completes, then cancel
    time.sleep(0.15)
    cancel_resp = web_client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_resp.status_code == 200
    cancelled_job = _wait_for_job(web_client, job_id)
    assert cancelled_job["status"] == "cancelled"

    resume_resp = web_client.post(f"/api/jobs/{job_id}/resume")
    assert resume_resp.status_code == 200
    completed_job = _wait_for_job(web_client, job_id)
    assert completed_job["status"] == "done"
    assert completed_job["progress"]["completed_pages"] == 12
    assert "multi_batch_translated.pdf" in completed_job["files"]

    with web_client.get(f"/download/{job_id}/multi_batch_translated.pdf") as dl_resp:
        assert dl_resp.status_code == 200
        assert dl_resp.data.startswith(b"%PDF")


def test_gui_pdf_controller_scan_abort_and_complete(tmp_path: Path):
    scan_pdf = _make_scan_pdf(tmp_path / "gui_scan.pdf")
    out_scan = tmp_path / "gui_scan_out.pdf"
    dummy = _DummyPdfTranslator()

    ctrl = PdfTranslationController()
    info = ctrl.inspect(str(scan_pdf))
    assert info["scan_pages"] == [1]

    ctrl.start(
        str(scan_pdf),
        str(out_scan),
        work_dir=str(tmp_path / "work_abort"),
        scan_policy="abort",
        translator=dummy,
    )
    assert ctrl.wait(timeout=10.0)
    ok, msg = ctrl.status_queue.get_nowait()
    assert ok is False
    assert "scan" in msg.lower()
    assert dummy.calls == []

    text_pdf = _make_text_pdf(tmp_path / "gui_text.pdf", pages=2)
    out_text = tmp_path / "gui_text_out.pdf"
    ctrl.start(
        str(text_pdf),
        str(out_text),
        work_dir=str(tmp_path / "work_ok"),
        scan_policy="keep_original",
        translator=dummy,
    )
    assert ctrl.wait(timeout=15.0)
    ok2, _ = ctrl.status_queue.get_nowait()
    assert ok2 is True
    assert out_text.is_file()


def test_web_pdf_page_range_and_gemini_404_fallback(web_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from unittest import mock
    from mathtranslate.engines.gemini_engine import GeminiTranslator

    # 1. Verify Gemini 404 auto-fallback from retired gemini-2.0-flash -> gemini-2.5-flash
    tr = GeminiTranslator(api_key="AIza-test", model="gemini-2.0-flash", min_interval=0.0)
    resp_404 = mock.MagicMock()
    resp_404.status_code = 404
    resp_200 = mock.MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"candidates": [{"content": {"parts": [{"text": "Bản dịch XMATHX_0"}]}}]}
    with mock.patch("mathtranslate.engines.gemini_engine.requests.post", side_effect=[resp_404, resp_200]) as m_post:
        out = tr.translate("Source XMATHX_0", language_to="vi", language_from="en")
        assert out == "Bản dịch XMATHX_0"
        assert tr.model == "gemini-2.5-flash"
        assert m_post.call_count == 2

    # 2. Verify page_range slicing for large textbooks
    pdf_path = _make_text_pdf(tmp_path / "huge_book.pdf", pages=6)
    dummy = _DummyPdfTranslator()
    monkeypatch.setattr("mathtranslate.engines.create_engine", lambda *a, **kw: dummy)
    monkeypatch.setattr("mathtranslate.translate.create_engine", lambda *a, **kw: dummy)

    with open(pdf_path, "rb") as fh:
        resp = web_client.post(
            "/api/translate/pdf",
            data={
                "file": (io.BytesIO(fh.read()), "huge_book.pdf"),
                "engine": "google",
                "l_from": "en",
                "l_to": "vi",
                "page_range": "2-4",
            },
            content_type="multipart/form-data",
        )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]
    done_job = _wait_for_job(web_client, job_id)
    assert done_job["status"] == "done"
    assert done_job["progress"]["total_pages"] == 3
    assert done_job["progress"]["completed_pages"] == 3
    assert "huge_book_p2-4_translated.pdf" in done_job["files"]
