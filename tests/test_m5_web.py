import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from mathtranslate.engines import AVAILABLE_ENGINES
from mathtranslate.pdf_compiler import CompileResult
import web.app as web_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    jobs_root = tmp_path / "web_jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setitem(web_app.app.config, "JOBS_ROOT", str(jobs_root))
    monkeypatch.setitem(web_app.app.config, "TESTING", True)
    with web_app.JOBS_LOCK:
        web_app.JOBS.clear()
    with web_app.app.test_client() as test_client:
        yield test_client


def _poll_until_finished(client, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last_payload = None
    while time.time() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        last_payload = resp.get_json()
        if last_payload["status"] in {"done", "error"}:
            return last_payload
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not finish within {timeout}s; last={last_payload}")


def test_index_page_and_editorial_design_tokens(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # No purple-blue AI gradient or broken nested <form>
    assert "bg-gradient" not in html.lower()
    assert "linear-gradient" not in html.lower()
    assert html.lower().count("<form") == 1
    assert html.lower().count("</form>") == 1
    assert "bootstrap" not in html.lower()

    # Check 3 mode tabs and 4-state UI anchors exist
    assert 'data-mode="arxiv"' in html
    assert 'data-mode="tex"' in html
    assert 'data-mode="zip"' in html
    assert 'data-state="idle"' in html

    # Verify CSS implements design-tokens.md values and mobile breakpoint
    with client.get("/static/css/style.css") as css_resp:
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
    for token_val in (
        "#FAF9F5",
        "#FFFFFF",
        "#F2F0E8",
        "#18181B",
        "#1B3A5C",
        "#9B2C2C",
        "#1F6E43",
        "#E2DFD5",
        "@media (max-width: 768px)",
    ):
        assert token_val in css
    assert "linear-gradient" not in css.lower()

    # Secret key must not be the hardcoded reference string
    assert web_app.app.secret_key != "some_secret_key"
    assert len(web_app.app.secret_key) >= 16


def test_api_meta_engines_and_languages(client):
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    data = resp.get_json()

    assert tuple(data["engines"]) == AVAILABLE_ENGINES
    assert len(data["engines"]) == 6
    assert data["defaults"]["l_from"] == "en"
    assert data["defaults"]["l_to"] == "vi"

    lang_codes = {item["code"] for item in data["languages"]}
    assert len(lang_codes) >= 60
    assert "en" in lang_codes
    assert "vi" in lang_codes
    assert "zh-CN" in lang_codes


def test_translate_tex_end_to_end_and_download(client, monkeypatch):
    def fake_translate_tex(input_path, output_path, engine, l_from, l_to, debug, nocache, threads):
        assert engine == "google"
        assert l_from == "en"
        assert l_to == "vi"
        with open(input_path, "r", encoding="utf-8") as src:
            raw = src.read()
        print("Mock translating tex file...")
        with open(output_path, "w", encoding="utf-8") as dst:
            dst.write(raw.replace("Hello", "Xin chao"))

    monkeypatch.setattr(web_app, "translate_single_tex_file", fake_translate_tex)

    resp = client.post(
        "/api/translate/tex",
        data={
            "engine": "google",
            "l_from": "en",
            "l_to": "vi",
            "compile_pdf": "false",
            "file": (io.BytesIO(b"\\documentclass{article}\\begin{document}Hello $x^2$\\end{document}"), "paper.tex"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    job = _poll_until_finished(client, job_id)
    assert job["status"] == "done"
    assert job["mode"] == "tex"
    assert job["pdf_status"] == "skipped"
    assert "Mock translating tex file..." in job["log"]
    assert "paper_translated.tex" in job["files"]

    with client.get(f"/download/{job_id}/paper_translated.tex") as dl:
        assert dl.status_code == 200
        assert "Xin chao $x^2$" in dl.get_data(as_text=True)
        assert "attachment" in dl.headers.get("Content-Disposition", "").lower()


def test_translate_tex_with_compile_pdf(client, monkeypatch):
    def fake_translate_tex(input_path, output_path, engine, l_from, l_to, debug, nocache, threads):
        with open(output_path, "w", encoding="utf-8") as dst:
            dst.write("\\documentclass{article}\\begin{document}Xin chao\\end{document}")

    def fake_compile(tex_path, work_dir=None, timeout=180):
        pdf_path = os.path.splitext(tex_path)[0] + ".pdf"
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 mock pdf")
        return CompileResult(
            status="success",
            pdf_path=pdf_path,
            log="xelatex ok",
            message="Compiled PDF successfully",
        )

    monkeypatch.setattr(web_app, "translate_single_tex_file", fake_translate_tex)
    monkeypatch.setattr(web_app, "compile_tex_to_pdf", fake_compile)

    resp = client.post(
        "/api/translate/tex",
        data={
            "engine": "google",
            "l_from": "en",
            "l_to": "vi",
            "compile_pdf": "true",
            "file": (io.BytesIO(b"\\documentclass{article}\\begin{document}Hi\\end{document}"), "demo.tex"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    job = _poll_until_finished(client, job_id)
    assert job["status"] == "done"
    assert job["pdf_status"] == "success"
    assert "demo_translated.tex" in job["files"]
    assert "demo_translated.pdf" in job["files"]

    with client.get(f"/download/{job_id}/demo_translated.pdf") as pdf_dl:
        assert pdf_dl.status_code == 200
        assert pdf_dl.data.startswith(b"%PDF-1.4")


def test_translate_zip_project_and_download(client, monkeypatch):
    def fake_translate_zip(zip_path, output_zip_path, engine, l_from, l_to, debug=False, nocache=False, threads=0):
        assert os.path.isfile(zip_path)
        assert engine == "gemini"
        print("Mock translating zip project...")
        with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("main.tex", "\\documentclass{article}\\begin{document}Da dich\\end{document}")
        return True

    monkeypatch.setattr(web_app, "translate_zip_project", fake_translate_zip)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("main.tex", "\\documentclass{article}\\begin{document}Original\\end{document}")
    zip_buf.seek(0)

    resp = client.post(
        "/api/translate/zip",
        data={
            "engine": "gemini",
            "l_from": "en",
            "l_to": "vi",
            "compile_pdf": "false",
            "file": (zip_buf, "my_paper.zip"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    job = _poll_until_finished(client, job_id)
    assert job["status"] == "done"
    assert job["mode"] == "zip"
    assert "my_paper_translated.zip" in job["files"]

    with client.get(f"/download/{job_id}/my_paper_translated.zip") as dl:
        assert dl.status_code == 200
        with zipfile.ZipFile(io.BytesIO(dl.data), "r") as zf:
            assert "main.tex" in zf.namelist()
            assert "Da dich" in zf.read("main.tex").decode("utf-8")


def test_translate_zip_reports_zip_slip_error(client, monkeypatch):
    def fake_zip_slip(*args, **kwargs):
        raise ValueError("Unsafe archive path traversal blocked: '../evil.txt'")

    monkeypatch.setattr(web_app, "translate_zip_project", fake_zip_slip)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("main.tex", "hello")
    zip_buf.seek(0)

    resp = client.post(
        "/api/translate/zip",
        data={
            "engine": "google",
            "l_from": "en",
            "l_to": "vi",
            "file": (zip_buf, "evil.zip"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 202
    job = _poll_until_finished(client, resp.get_json()["job_id"])
    assert job["status"] == "error"
    assert "traversal" in (job["error"] or "").lower()


def test_translate_arxiv_end_to_end(client, monkeypatch):
    def fake_arxiv_main(args=None, require_updated=False):
        assert args[0] == "2403.08441"
        out_idx = args.index("-o") + 1
        out_zip = args[out_idx]
        with zipfile.ZipFile(out_zip, "w") as zf:
            zf.writestr("main.tex", "translated arxiv")
        return True

    monkeypatch.setattr(web_app, "translate_arxiv_main", fake_arxiv_main)

    resp = client.post(
        "/api/translate/arxiv",
        json={"arxiv_id": "https://arxiv.org/abs/2403.08441", "engine": "google", "l_from": "en", "l_to": "vi", "compile_pdf": False},
    )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    job = _poll_until_finished(client, job_id)
    assert job["status"] == "done"
    assert "2403.08441_translated.zip" in job["files"]

    with client.get(f"/download/{job_id}/2403.08441_translated.zip") as dl:
        assert dl.status_code == 200


def test_invalid_engine_and_bad_inputs_return_400(client):
    resp_arxiv = client.post(
        "/api/translate/arxiv",
        json={"arxiv_id": "2403.08441", "engine": "invalid_engine", "l_from": "en", "l_to": "vi"},
    )
    assert resp_arxiv.status_code == 400
    assert "invalid" in resp_arxiv.get_json()["error"].lower()

    resp_tex = client.post(
        "/api/translate/tex",
        data={
            "engine": "unknown",
            "file": (io.BytesIO(b"hi"), "doc.tex"),
        },
        content_type="multipart/form-data",
    )
    assert resp_tex.status_code == 400

    resp_bad_ext = client.post(
        "/api/translate/tex",
        data={
            "engine": "google",
            "file": (io.BytesIO(b"hi"), "notes.txt"),
        },
        content_type="multipart/form-data",
    )
    assert resp_bad_ext.status_code == 400

    resp_bad_arxiv = client.post(
        "/api/translate/arxiv",
        json={"arxiv_id": "../etc/passwd", "engine": "google"},
    )
    assert resp_bad_arxiv.status_code == 400


def test_download_blocks_path_traversal(client, tmp_path):
    # Create a real job with a file and a sibling secret file outside output_dir
    params = {"engine": "google", "l_from": "en", "l_to": "vi", "compile_pdf": False}
    job_id, _, output_dir = web_app._create_job("tex", params)
    secret_path = os.path.join(os.path.dirname(output_dir), "secret.txt")
    with open(secret_path, "w", encoding="utf-8") as f:
        f.write("TOP_SECRET")
    ok_path = os.path.join(output_dir, "safe.tex")
    with open(ok_path, "w", encoding="utf-8") as f:
        f.write("OK")

    # Legitimate file works
    with client.get(f"/download/{job_id}/safe.tex") as ok_resp:
        assert ok_resp.status_code == 200
        assert ok_resp.data == b"OK"

    # Path traversal variants must be rejected with 400 or 404
    for bad_url in (
        f"/download/{job_id}/../secret.txt",
        f"/download/{job_id}/..%2Fsecret.txt",
        f"/download/{job_id}/..%5Csecret.txt",
        f"/download/{job_id}/subdir/safe.tex",
    ):
        resp = client.get(bad_url)
        assert resp.status_code in (400, 404), f"Expected 400/404 for {bad_url}, got {resp.status_code}"
        assert b"TOP_SECRET" not in resp.data


def test_cleanup_expired_jobs_and_daemon_thread(tmp_path):
    jobs_root = tmp_path / "cleanup_root"
    old_dir = jobs_root / "old_job"
    active_dir = jobs_root / "active_job"
    fresh_dir = jobs_root / "fresh_job"
    old_dir.mkdir(parents=True)
    active_dir.mkdir(parents=True)
    fresh_dir.mkdir(parents=True)

    old_ts = time.time() - 7200  # 2 hours ago
    os.utime(old_dir, (old_ts, old_ts))
    os.utime(active_dir, (old_ts, old_ts))

    with web_app.JOBS_LOCK:
        web_app.JOBS["active_job"] = {"id": "active_job", "status": "running", "updated_at": old_ts}
    try:
        removed = web_app.cleanup_expired_jobs(jobs_root=str(jobs_root), max_age_seconds=3600)
        assert removed == 1
        assert not old_dir.exists()
        assert active_dir.exists()
        assert fresh_dir.exists()
    finally:
        with web_app.JOBS_LOCK:
            web_app.JOBS.pop("active_job", None)

    thread = web_app.start_cleanup_thread()
    assert thread.is_alive()
    assert thread.daemon is True
