"""Flask Local Web UI for MathTranslate v2."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from mathtranslate import __version__
from mathtranslate.engines import AVAILABLE_ENGINES
from mathtranslate.pdf_compiler import compile_tex_to_pdf, compile_zip_to_pdf
from mathtranslate.translate import translate_single_tex_file
from mathtranslate.translate_arxiv import (
    main as translate_arxiv_main,
    translate_zip_project,
)
from mathtranslate.utils import normalize_arxiv_id, parse_language_options

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.secret_key = os.environ.get("MATHTRANSLATE_SECRET_KEY") or secrets.token_hex(32)
app.config["JOBS_ROOT"] = os.environ.get("MATHTRANSLATE_WEB_JOBS_DIR") or os.path.join(
    tempfile.gettempdir(), "mathtranslate_web_jobs"
)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
app.config["JOB_TTL_SECONDS"] = 3600

DEFAULT_ENGINE = "google"
DEFAULT_L_FROM = "en"
DEFAULT_L_TO = "vi"

_ARXIV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-/]{1,63}$")
_JOB_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")

EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mathtranslate-job")
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

_CLEANUP_LOCK = threading.Lock()
_CLEANUP_STOP_EVENT = threading.Event()
_CLEANUP_THREAD: threading.Thread | None = None

_CAPTURE_LOCK = threading.Lock()
_CAPTURE_REFCOUNT = 0
_ORIG_STDOUT = None
_ORIG_STDERR = None
_THREAD_LOCAL = threading.local()


class _ThreadLocalStreamRouter:
    """Routes stdout/stderr writes from worker threads to their own job log."""

    def __init__(self, fallback_stream: Any) -> None:
        self._fallback = fallback_stream

    def write(self, data: str) -> int:
        if not data:
            return 0
        callback: Callable[[str], None] | None = getattr(_THREAD_LOCAL, "log_cb", None)
        if callback is not None:
            callback(data)
            return len(data)
        return self._fallback.write(data)

    def flush(self) -> None:
        callback = getattr(_THREAD_LOCAL, "log_cb", None)
        if callback is None:
            self._fallback.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fallback, name)


@contextmanager
def _capture_thread_output(log_cb: Callable[[str], None]):
    """Capture stdout/stderr only for the calling thread while a job runs."""
    global _CAPTURE_REFCOUNT, _ORIG_STDOUT, _ORIG_STDERR

    with _CAPTURE_LOCK:
        if _CAPTURE_REFCOUNT == 0:
            _ORIG_STDOUT = sys.stdout
            _ORIG_STDERR = sys.stderr
            sys.stdout = _ThreadLocalStreamRouter(_ORIG_STDOUT)
            sys.stderr = _ThreadLocalStreamRouter(_ORIG_STDERR)
        _CAPTURE_REFCOUNT += 1

    prev_cb = getattr(_THREAD_LOCAL, "log_cb", None)
    _THREAD_LOCAL.log_cb = log_cb
    try:
        yield
    finally:
        _THREAD_LOCAL.log_cb = prev_cb
        with _CAPTURE_LOCK:
            _CAPTURE_REFCOUNT -= 1
            if _CAPTURE_REFCOUNT == 0 and _ORIG_STDOUT is not None and _ORIG_STDERR is not None:
                sys.stdout = _ORIG_STDOUT
                sys.stderr = _ORIG_STDERR
                _ORIG_STDOUT = None
                _ORIG_STDERR = None


def parse_languages() -> list[dict[str, str]]:
    """Return sorted {code, name} entries from mathtranslate.utils."""
    return [{"code": code, "name": name} for name, code in parse_language_options()]


LANGUAGES = parse_languages()
VALID_LANGUAGE_CODES = {item["code"] for item in LANGUAGES} | {"zh"}


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _get_payload_field(name: str, default: Any = None) -> Any:
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if name in data:
            return data[name]
    if name in request.form:
        return request.form[name]
    return default


def _validate_translation_params() -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    raw_engine = _get_payload_field("engine", DEFAULT_ENGINE)
    engine = str(raw_engine or "").strip().lower()
    if engine not in AVAILABLE_ENGINES:
        return None, (
            jsonify(
                {
                    "error": f"Invalid translation engine '{raw_engine}'. "
                    f"Available engines: {', '.join(AVAILABLE_ENGINES)}"
                }
            ),
            400,
        )

    l_from = str(_get_payload_field("l_from", DEFAULT_L_FROM) or "").strip()
    l_to = str(_get_payload_field("l_to", DEFAULT_L_TO) or "").strip()
    if not l_from or l_from not in VALID_LANGUAGE_CODES:
        return None, (jsonify({"error": f"Unsupported source language code '{l_from}'."}), 400)
    if not l_to or l_to not in VALID_LANGUAGE_CODES:
        return None, (jsonify({"error": f"Unsupported target language code '{l_to}'."}), 400)

    compile_pdf = _parse_bool(_get_payload_field("compile_pdf", False), default=False)
    return {
        "engine": engine,
        "l_from": l_from,
        "l_to": l_to,
        "compile_pdf": compile_pdf,
    }, None


def _validate_arxiv_id(raw_id: Any) -> str | None:
    text = normalize_arxiv_id(str(raw_id or ""))
    if not text or ".." in text or "\\" in text or "//" in text or text.endswith("/"):
        return None
    if not _ARXIV_ID_RE.match(text):
        return None
    return text


def _sanitize_upload_filename(raw_name: str, expected_ext: str, fallback_stem: str) -> str | None:
    if not raw_name or not raw_name.lower().endswith(expected_ext.lower()):
        return None
    cleaned = secure_filename(raw_name)
    if not cleaned:
        return f"{fallback_stem}{expected_ext}"
    if not cleaned.lower().endswith(expected_ext.lower()):
        stem = cleaned.rsplit(".", 1)[0] if "." in cleaned else cleaned
        stem = stem.strip("._") or fallback_stem
        return f"{stem}{expected_ext}"
    stem = cleaned[: -len(expected_ext)].strip("._")
    if not stem:
        return f"{fallback_stem}{expected_ext}"
    return f"{stem}{expected_ext}"


def _ensure_jobs_root() -> str:
    jobs_root = app.config["JOBS_ROOT"]
    os.makedirs(jobs_root, exist_ok=True)
    return jobs_root


def _create_job(mode: str, params: dict[str, Any]) -> tuple[str, str, str]:
    job_id = uuid.uuid4().hex[:16]
    jobs_root = _ensure_jobs_root()
    job_dir = os.path.join(jobs_root, job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    now = time.time()
    record = {
        "id": job_id,
        "status": "queued",
        "mode": mode,
        "engine": params["engine"],
        "l_from": params["l_from"],
        "l_to": params["l_to"],
        "compile_pdf": params["compile_pdf"],
        "created_at": now,
        "updated_at": now,
        "log": f"[{mode}] Job {job_id} queued (engine={params['engine']}, {params['l_from']} -> {params['l_to']}).\n",
        "files": [],
        "pdf_status": None,
        "pdf_message": None,
        "error": None,
        "_job_dir": job_dir,
        "_output_dir": output_dir,
    }
    with JOBS_LOCK:
        JOBS[job_id] = record
    return job_id, input_dir, output_dir


def _append_log(job_id: str, text: str) -> None:
    if not text:
        return
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job["log"] += text
            job["updated_at"] = time.time()


def _update_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(fields)
            job["updated_at"] = time.time()


def _serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
        "mode": job["mode"],
        "engine": job["engine"],
        "l_from": job["l_from"],
        "l_to": job["l_to"],
        "compile_pdf": job["compile_pdf"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "log": job["log"],
        "files": list(job["files"]),
        "pdf_status": job["pdf_status"],
        "pdf_message": job["pdf_message"],
        "error": job["error"],
    }


def _compile_archive_to_pdf(
    job_id: str,
    zip_path: str,
    output_dir: str,
    pdf_filename: str,
) -> tuple[str, str, str | None]:
    target_pdf = os.path.join(output_dir, pdf_filename)
    _append_log(job_id, "[pdf] Compiling archive with XeLaTeX...\n")
    result = compile_zip_to_pdf(zip_path, target_pdf)
    _append_log(job_id, f"[pdf] {result.message}\n")
    if result.log:
        _append_log(job_id, f"{result.log}\n")
    if result.status == "success" and result.pdf_path and os.path.isfile(result.pdf_path):
        return "success", result.message, pdf_filename
    return result.status, result.message, None


def _run_tex_job(
    job_id: str,
    input_path: str,
    output_dir: str,
    output_filename: str,
    params: dict[str, Any],
) -> None:
    _update_job(job_id, status="running")
    output_path = os.path.join(output_dir, output_filename)
    try:
        with _capture_thread_output(lambda chunk: _append_log(job_id, chunk)):
            _append_log(job_id, f"[tex] Translating {os.path.basename(input_path)}...\n")
            threads = 1 if params["engine"] == "tencent" else 0
            translate_single_tex_file(
                input_path,
                output_path,
                params["engine"],
                params["l_from"],
                params["l_to"],
                False,
                False,
                threads,
            )

        if not os.path.isfile(output_path):
            raise RuntimeError("Translation finished without producing the output .tex file.")

        files = [output_filename]
        pdf_status = "skipped"
        pdf_message = None

        if params["compile_pdf"]:
            _append_log(job_id, f"[pdf] Compiling {output_filename} with XeLaTeX...\n")
            result = compile_tex_to_pdf(output_path, work_dir=output_dir)
            pdf_status = result.status
            pdf_message = result.message
            _append_log(job_id, f"[pdf] {result.message}\n")
            if result.log:
                _append_log(job_id, f"{result.log}\n")
            if result.status == "success" and result.pdf_path and os.path.isfile(result.pdf_path):
                pdf_name = os.path.basename(result.pdf_path)
                if pdf_name not in files:
                    files.append(pdf_name)

        _append_log(job_id, "[tex] Completed successfully.\n")
        _update_job(
            job_id,
            status="done",
            files=files,
            pdf_status=pdf_status,
            pdf_message=pdf_message,
            error=None,
        )
    except (Exception, SystemExit) as exc:
        err_msg = str(exc) or exc.__class__.__name__
        _append_log(job_id, f"[error] {err_msg}\n{traceback.format_exc()}\n")
        _update_job(job_id, status="error", error=err_msg)


def _run_zip_job(
    job_id: str,
    input_zip: str,
    output_dir: str,
    output_filename: str,
    params: dict[str, Any],
) -> None:
    _update_job(job_id, status="running")
    output_zip = os.path.join(output_dir, output_filename)
    try:
        with _capture_thread_output(lambda chunk: _append_log(job_id, chunk)):
            _append_log(job_id, f"[zip] Extracting and translating {os.path.basename(input_zip)}...\n")
            threads = 1 if params["engine"] == "tencent" else 0
            ok = translate_zip_project(
                input_zip,
                output_zip,
                params["engine"],
                params["l_from"],
                params["l_to"],
                debug=False,
                nocache=False,
                threads=threads,
            )
        if not ok or not os.path.isfile(output_zip):
            raise RuntimeError("No complete .tex document was found inside the uploaded ZIP archive.")

        files = [output_filename]
        pdf_status = "skipped"
        pdf_message = None

        if params["compile_pdf"]:
            pdf_name = os.path.splitext(output_filename)[0] + ".pdf"
            pdf_status, pdf_message, produced_pdf = _compile_archive_to_pdf(
                job_id, output_zip, output_dir, pdf_name
            )
            if produced_pdf:
                files.append(produced_pdf)

        _append_log(job_id, "[zip] Completed successfully.\n")
        _update_job(
            job_id,
            status="done",
            files=files,
            pdf_status=pdf_status,
            pdf_message=pdf_message,
            error=None,
        )
    except (Exception, SystemExit) as exc:
        err_msg = str(exc) or exc.__class__.__name__
        _append_log(job_id, f"[error] {err_msg}\n{traceback.format_exc()}\n")
        _update_job(job_id, status="error", error=err_msg)


def _run_arxiv_job(
    job_id: str,
    arxiv_id: str,
    output_dir: str,
    output_filename: str,
    params: dict[str, Any],
) -> None:
    _update_job(job_id, status="running")
    output_zip = os.path.join(output_dir, output_filename)
    try:
        cli_args = [
            arxiv_id,
            "-o",
            output_zip,
            "-engine",
            params["engine"],
            "-from",
            params["l_from"],
            "-to",
            params["l_to"],
        ]
        with _capture_thread_output(lambda chunk: _append_log(job_id, chunk)):
            _append_log(job_id, f"[arxiv] Downloading and translating arXiv:{arxiv_id}...\n")
            ok = translate_arxiv_main(cli_args, require_updated=False)

        if not ok or not os.path.isfile(output_zip):
            raise RuntimeError(
                f"Could not translate arXiv:{arxiv_id}. Source may be PDF-only or unavailable."
            )

        files = [output_filename]
        pdf_status = "skipped"
        pdf_message = None

        if params["compile_pdf"]:
            pdf_name = os.path.splitext(output_filename)[0] + ".pdf"
            pdf_status, pdf_message, produced_pdf = _compile_archive_to_pdf(
                job_id, output_zip, output_dir, pdf_name
            )
            if produced_pdf:
                files.append(produced_pdf)

        _append_log(job_id, "[arxiv] Completed successfully.\n")
        _update_job(
            job_id,
            status="done",
            files=files,
            pdf_status=pdf_status,
            pdf_message=pdf_message,
            error=None,
        )
    except (Exception, SystemExit) as exc:
        err_msg = str(exc) or exc.__class__.__name__
        _append_log(job_id, f"[error] {err_msg}\n{traceback.format_exc()}\n")
        _update_job(job_id, status="error", error=err_msg)


def cleanup_expired_jobs(
    jobs_root: str | None = None,
    max_age_seconds: int | None = None,
) -> int:
    """Delete job folders and finished job records older than max_age_seconds."""
    root = jobs_root or app.config["JOBS_ROOT"]
    ttl = app.config["JOB_TTL_SECONDS"] if max_age_seconds is None else max_age_seconds
    cutoff = time.time() - ttl
    removed = 0

    with JOBS_LOCK:
        active_ids = {
            jid
            for jid, rec in JOBS.items()
            if rec["status"] not in {"done", "error"}
        }

    if os.path.isdir(root):
        for entry in os.listdir(root):
            if entry in active_ids:
                continue
            entry_path = os.path.join(root, entry)
            try:
                mtime = os.path.getmtime(entry_path)
            except OSError:
                continue
            if mtime < cutoff:
                if os.path.isdir(entry_path):
                    shutil.rmtree(entry_path, ignore_errors=True)
                    removed += 1
                else:
                    try:
                        os.unlink(entry_path)
                        removed += 1
                    except OSError:
                        pass

    with JOBS_LOCK:
        expired_ids = [
            jid
            for jid, rec in JOBS.items()
            if rec["status"] in {"done", "error"} and rec["updated_at"] < cutoff
        ]
        for jid in expired_ids:
            JOBS.pop(jid, None)

    return removed


def _cleanup_worker(interval_seconds: int) -> None:
    while not _CLEANUP_STOP_EVENT.wait(interval_seconds):
        try:
            cleanup_expired_jobs()
        except Exception:
            pass


def start_cleanup_thread(interval_seconds: int = 600) -> threading.Thread:
    """Start the background daemon cleanup thread once."""
    global _CLEANUP_THREAD
    with _CLEANUP_LOCK:
        if _CLEANUP_THREAD is not None and _CLEANUP_THREAD.is_alive():
            return _CLEANUP_THREAD
        _CLEANUP_STOP_EVENT.clear()
        thread = threading.Thread(
            target=_cleanup_worker,
            args=(interval_seconds,),
            name="mathtranslate-cleanup",
            daemon=True,
        )
        thread.start()
        _CLEANUP_THREAD = thread
        return thread


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        engines=list(AVAILABLE_ENGINES),
        languages=LANGUAGES,
        default_engine=DEFAULT_ENGINE,
        default_l_from=DEFAULT_L_FROM,
        default_l_to=DEFAULT_L_TO,
        version=__version__,
    )


@app.route("/api/meta", methods=["GET"])
def api_meta():
    return jsonify(
        {
            "engines": list(AVAILABLE_ENGINES),
            "languages": LANGUAGES,
            "defaults": {
                "engine": DEFAULT_ENGINE,
                "l_from": DEFAULT_L_FROM,
                "l_to": DEFAULT_L_TO,
                "compile_pdf": False,
            },
            "version": __version__,
        }
    )


@app.route("/api/translate/arxiv", methods=["POST"])
def api_translate_arxiv():
    params, err = _validate_translation_params()
    if err is not None:
        return err

    arxiv_id = _validate_arxiv_id(_get_payload_field("arxiv_id", ""))
    if not arxiv_id:
        return jsonify({"error": "Please provide a valid arXiv ID (for example: 2403.08441)."}), 400

    job_id, _, output_dir = _create_job("arxiv", params)
    safe_stem = secure_filename(arxiv_id.replace("/", "-")) or "arxiv_paper"
    output_filename = f"{safe_stem}_translated.zip"

    EXECUTOR.submit(_run_arxiv_job, job_id, arxiv_id, output_dir, output_filename, params)
    return jsonify({"job_id": job_id, "status": "queued", "mode": "arxiv"}), 202


@app.route("/api/translate/tex", methods=["POST"])
def api_translate_tex():
    params, err = _validate_translation_params()
    if err is not None:
        return err

    uploaded = request.files.get("file") or request.files.get("tex_file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Please select a .tex file to upload."}), 400

    safe_name = _sanitize_upload_filename(uploaded.filename, ".tex", "document")
    if safe_name is None:
        return jsonify({"error": "Only .tex files are accepted in single-file mode."}), 400

    job_id, input_dir, output_dir = _create_job("tex", params)
    input_path = os.path.join(input_dir, safe_name)
    uploaded.save(input_path)

    stem = os.path.splitext(safe_name)[0]
    output_filename = f"{stem}_translated.tex"

    EXECUTOR.submit(_run_tex_job, job_id, input_path, output_dir, output_filename, params)
    return jsonify({"job_id": job_id, "status": "queued", "mode": "tex"}), 202


@app.route("/api/translate/zip", methods=["POST"])
def api_translate_zip():
    params, err = _validate_translation_params()
    if err is not None:
        return err

    uploaded = request.files.get("file") or request.files.get("zip_file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Please select a .zip archive to upload."}), 400

    safe_name = _sanitize_upload_filename(uploaded.filename, ".zip", "project")
    if safe_name is None:
        return jsonify({"error": "Only .zip archives are accepted in project mode."}), 400

    job_id, input_dir, output_dir = _create_job("zip", params)
    input_path = os.path.join(input_dir, safe_name)
    uploaded.save(input_path)

    stem = os.path.splitext(safe_name)[0]
    output_filename = f"{stem}_translated.zip"

    EXECUTOR.submit(_run_zip_job, job_id, input_path, output_dir, output_filename, params)
    return jsonify({"job_id": job_id, "status": "queued", "mode": "zip"}), 202


@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_get_job(job_id: str):
    if not _JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "Invalid job ID."}), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        snapshot = _serialize_job(job)
    return jsonify(snapshot)


@app.route("/download/<job_id>/<path:filename>", methods=["GET"])
def download_artifact(job_id: str, filename: str):
    if not _JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "Invalid job ID."}), 400

    if (
        not filename
        or ".." in filename
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        return jsonify({"error": "Invalid filename."}), 400

    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
        return jsonify({"error": "Invalid filename."}), 400

    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        output_dir = job["_output_dir"]

    real_output_dir = os.path.realpath(output_dir)
    target_path = os.path.realpath(os.path.join(real_output_dir, safe_name))
    try:
        inside = os.path.commonpath([real_output_dir, target_path]) == real_output_dir
    except ValueError:
        inside = False

    if not inside:
        return jsonify({"error": "Access denied."}), 400

    if not os.path.isfile(target_path):
        return jsonify({"error": "Requested file not found."}), 404

    return send_from_directory(real_output_dir, safe_name, as_attachment=True)


def run_server(host: str = "127.0.0.1", port: int = 5000) -> None:
    """Start the cleanup daemon thread and launch the local Flask web server."""
    _ensure_jobs_root()
    start_cleanup_thread()
    app.run(host=host, port=port, debug=False)


# Ensure cleanup thread is active even when run via WSGI or imported directly
start_cleanup_thread()

if __name__ == "__main__":
    run_server()
