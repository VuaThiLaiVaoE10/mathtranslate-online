"""PDF-M2 chunked batch PDF translation engine with SQLite TM, atomic checkpoints, progressive partial.pdf, and cancel/resume."""

from __future__ import annotations

import concurrent.futures
import copy
from dataclasses import dataclass, field
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Sequence
import urllib.error

import pymupdf
import requests

from .engines import BaseTranslator, ENGINE_REGISTRY
from .pdf_doc import (
    DEFAULT_EN_VI_GLOSSARY,
    PageClassification,
    ScanPolicy,
    ScanPagesDetectedError,
    _normalize_toc_levels,
    classify_pdf_pages,
    extract_ambiguous_terms,
    translate_pdf_pages_in_doc,
)
from .pdf_qa import save_qa_report_json, verify_translated_pdf
from .translate import TextTranslator

_ALPHA_RE = re.compile(r".*[a-zA-Z].*", re.DOTALL)


class BatchCancelledError(RuntimeError):
    """Raised internally when an in-flight batch is aborted by cancel()."""


def compute_glossary_hash(glossary: dict[str, str] | None) -> str:
    """Return canonical SHA-256 hex digest of sorted {k.strip(): v.strip()} pairs."""
    normalized: dict[str, str] = {}
    if glossary:
        for raw_k, raw_v in glossary.items():
            ks = str(raw_k).strip()
            vs = str(raw_v).strip()
            if not ks:
                continue
            for existing_k in [ek for ek in normalized if ek.lower() == ks.lower()]:
                del normalized[existing_k]
            normalized[ks] = vs
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_density_batches(
    page_indices: Sequence[int],
    char_counts: dict[int, int] | Sequence[int] | Sequence[PageClassification] | dict[int, PageClassification],
    *,
    min_batch_pages: int = 5,
    max_batch_pages: int = 10,
    target_batch_chars: int = 14000,
) -> list[list[int]]:
    """Partition `page_indices` into 5-10 page batches auto-adjusted by page text density."""
    if min_batch_pages < 1:
        raise ValueError(f"min_batch_pages must be >= 1, got {min_batch_pages}")
    if max_batch_pages < min_batch_pages:
        raise ValueError(
            f"max_batch_pages ({max_batch_pages}) must be >= min_batch_pages ({min_batch_pages})"
        )

    pages = list(page_indices)
    if not pages:
        return []
    if len(pages) <= min_batch_pages:
        return [pages]

    def _get_chars(pno: int) -> int:
        if isinstance(char_counts, dict):
            val = char_counts.get(pno, 0)
        elif 0 <= pno < len(char_counts):
            val = char_counts[pno]
        else:
            val = 0
        if hasattr(val, "text_chars"):
            return max(0, int(val.text_chars))
        return max(0, int(val))

    batches: list[list[int]] = []
    cur: list[int] = []
    cur_chars = 0
    for pno in pages:
        p_chars = _get_chars(pno)
        if cur and (
            len(cur) >= max_batch_pages
            or (len(cur) >= min_batch_pages and cur_chars + p_chars > target_batch_chars)
        ):
            batches.append(cur)
            cur = []
            cur_chars = 0
        cur.append(pno)
        cur_chars += p_chars
    if cur:
        batches.append(cur)

    # Rebalance short trailing batch so all batches sit within [min_batch_pages, max_batch_pages]
    if len(batches) >= 2 and len(batches[-1]) < min_batch_pages:
        last = batches[-1]
        prev = batches[-2]
        if len(prev) + len(last) <= max_batch_pages:
            prev.extend(last)
            batches.pop()
        elif len(prev) + len(last) >= 2 * min_batch_pages:
            combined = prev + last
            mid = len(combined) // 2
            batches[-2] = combined[:mid]
            batches[-1] = combined[mid:]

    return batches


class SQLiteTranslationMemory:
    """Thread-safe persistent SQLite translation memory (WAL mode) keyed by SHA-256."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_memory (
                    tm_key TEXT PRIMARY KEY,
                    source_text TEXT NOT NULL,
                    translated_text TEXT NOT NULL,
                    glossary_hash TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    l_from TEXT NOT NULL,
                    l_to TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.commit()

    @staticmethod
    def make_key(
        text: str,
        glossary_hash: str,
        engine: str,
        l_from: str,
        l_to: str,
    ) -> str:
        payload = json.dumps(
            [text, glossary_hash, engine, l_from, l_to],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def lookup(
        self,
        text: str,
        glossary_hash: str,
        engine: str,
        l_from: str,
        l_to: str,
    ) -> str | None:
        key = self.make_key(text, glossary_hash, engine, l_from, l_to)
        with self._lock:
            if self._closed:
                return None
            cur = self._conn.execute(
                "SELECT translated_text FROM translation_memory WHERE tm_key = ?",
                (key,),
            )
            row = cur.fetchone()
            return row[0] if row is not None else None

    def store(
        self,
        text: str,
        translated: str,
        glossary_hash: str,
        engine: str,
        l_from: str,
        l_to: str,
    ) -> None:
        key = self.make_key(text, glossary_hash, engine, l_from, l_to)
        now = time.time()
        with self._lock:
            if self._closed:
                return
            self._conn.execute(
                """
                INSERT OR REPLACE INTO translation_memory (
                    tm_key, source_text, translated_text, glossary_hash, engine, l_from, l_to, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, text, translated, glossary_hash, engine, l_from, l_to, now),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            if self._closed:
                return 0
            cur = self._conn.execute("SELECT COUNT(*) FROM translation_memory")
            row = cur.fetchone()
            return int(row[0]) if row is not None else 0

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> SQLiteTranslationMemory:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def inspect_pdf(input_path: str | Path, *, scan_pages_all: bool = True) -> dict[str, Any]:
    """Preflight inspection returning page classifications, counts, scan pages, and ambiguous terms."""
    with pymupdf.open(str(input_path)) as doc:
        cls_list = classify_pdf_pages(doc)
        counts = {"TEXT": 0, "SCAN": 0, "MIXED": 0}
        for c in cls_list:
            counts[c.kind] = counts.get(c.kind, 0) + 1
        scan_pages = [c.page_index for c in cls_list if c.kind == "SCAN"]
        ambiguous = extract_ambiguous_terms(doc, scan_pages_all=scan_pages_all)
        return {
            "total_pages": len(cls_list),
            "classifications": [
                {
                    "page_index": c.page_index,
                    "kind": c.kind,
                    "text_chars": c.text_chars,
                    "image_count": c.image_count,
                    "image_area_ratio": c.image_area_ratio,
                }
                for c in cls_list
            ],
            "counts": counts,
            "scan_pages": scan_pages,
            "ambiguous_terms": ambiguous,
            "default_glossary": dict(DEFAULT_EN_VI_GLOSSARY),
        }


@dataclass
class BatchCheckpoint:
    """Atomic batch translation progress checkpoint persisted as checkpoint.json."""

    job_id: str
    input_path: str
    output_path: str
    partial_path: str
    tm_path: str
    engine: str
    l_from: str
    l_to: str
    scan_policy: str
    glossary_hash: str
    glossary: dict[str, str]
    status: str
    total_pages: int
    batches: list[list[int]]
    completed_batches: list[int] = field(default_factory=list)
    completed_pages: list[int] = field(default_factory=list)
    pending_pages: list[int] = field(default_factory=list)
    overflow_pages: list[int] = field(default_factory=list)
    scan_pages: list[int] = field(default_factory=list)
    error_pages: dict[str, str] = field(default_factory=dict)
    pages_per_sec: float = 0.0
    eta_seconds: float | None = None
    elapsed_seconds: float = 0.0
    updated_at: float = field(default_factory=time.time)
    # Runtime settings persisted for cross-restart from_checkpoint()
    min_scale: float = 0.68
    max_workers: int = 2
    min_batch_pages: int = 5
    max_batch_pages: int = 10
    run_qa: bool = True
    qa_sample_pages: int | None = 40

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "input_path": self.input_path,
            "output_path": self.output_path,
            "partial_path": self.partial_path,
            "tm_path": self.tm_path,
            "engine": self.engine,
            "l_from": self.l_from,
            "l_to": self.l_to,
            "scan_policy": self.scan_policy,
            "glossary_hash": self.glossary_hash,
            "glossary": dict(self.glossary),
            "status": self.status,
            "total_pages": self.total_pages,
            "batches": [list(b) for b in self.batches],
            "completed_batches": list(self.completed_batches),
            "completed_pages": list(self.completed_pages),
            "pending_pages": list(self.pending_pages),
            "overflow_pages": list(self.overflow_pages),
            "scan_pages": list(self.scan_pages),
            "error_pages": dict(self.error_pages),
            "pages_per_sec": self.pages_per_sec,
            "eta_seconds": self.eta_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "updated_at": self.updated_at,
            "min_scale": self.min_scale,
            "max_workers": self.max_workers,
            "min_batch_pages": self.min_batch_pages,
            "max_batch_pages": self.max_batch_pages,
            "run_qa": self.run_qa,
            "qa_sample_pages": self.qa_sample_pages,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchCheckpoint:
        return cls(
            job_id=str(data["job_id"]),
            input_path=str(data["input_path"]),
            output_path=str(data["output_path"]),
            partial_path=str(data["partial_path"]),
            tm_path=str(data["tm_path"]),
            engine=str(data.get("engine", "google")),
            l_from=str(data.get("l_from", "en")),
            l_to=str(data.get("l_to", "vi")),
            scan_policy=str(data.get("scan_policy", "abort")),
            glossary_hash=str(data.get("glossary_hash", compute_glossary_hash(None))),
            glossary=dict(data.get("glossary") or {}),
            status=str(data.get("status", "pending")),
            total_pages=int(data["total_pages"]),
            batches=[list(b) for b in data.get("batches", [])],
            completed_batches=[int(b) for b in data.get("completed_batches", [])],
            completed_pages=[int(p) for p in data.get("completed_pages", [])],
            pending_pages=[int(p) for p in data.get("pending_pages", [])],
            overflow_pages=[int(p) for p in data.get("overflow_pages", [])],
            scan_pages=[int(p) for p in data.get("scan_pages", [])],
            error_pages={str(k): str(v) for k, v in (data.get("error_pages") or {}).items()},
            pages_per_sec=float(data.get("pages_per_sec", 0.0)),
            eta_seconds=(
                None if data.get("eta_seconds") is None else float(data["eta_seconds"])
            ),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            updated_at=float(data.get("updated_at", time.time())),
            min_scale=float(data.get("min_scale", 0.68)),
            max_workers=int(data.get("max_workers", 2)),
            min_batch_pages=int(data.get("min_batch_pages", 5)),
            max_batch_pages=int(data.get("max_batch_pages", 10)),
            run_qa=bool(data.get("run_qa", True)),
            qa_sample_pages=(
                None if data.get("qa_sample_pages") is None else int(data["qa_sample_pages"])
            ),
        )


def _atomic_write_json(dest_path: Path, payload: dict[str, Any]) -> None:
    dest_abs = os.path.abspath(str(dest_path))
    os.makedirs(os.path.dirname(dest_abs) or ".", exist_ok=True)
    tmp_path = f"{dest_abs}.tmp_{os.getpid()}_{threading.get_ident()}.json"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, dest_abs)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


class _FallbackClassifier(BaseTranslator):
    """Helper using BaseTranslator's HTTP/network retry classification for non-BaseTranslator mocks."""

    name = "fallback"

    def translate(self, text: str, language_to: str, language_from: str) -> str:
        raise NotImplementedError


_FALLBACK_CLASSIFIER = _FallbackClassifier()


class _RetryingTranslatorProxy:
    """Thread-safe exponential-backoff & cancel-aware proxy around TextTranslator or mock translators."""

    def __init__(
        self,
        inner: Any,
        *,
        engine: str,
        l_from: str,
        l_to: str,
        max_retries: int,
        base_backoff: float,
        cancel_event: threading.Event,
        stats_lock: threading.Lock | None = None,
    ) -> None:
        self._inner = inner
        self.engine = getattr(inner, "engine", engine)
        self.language_from = getattr(inner, "language_from", l_from)
        self.language_to = getattr(inner, "language_to", l_to)
        self.translator = getattr(inner, "translator", inner)
        self.max_retries = max(0, int(max_retries))
        self.base_backoff = max(0.0, float(base_backoff))
        self._cancel_event = cancel_event
        self._stats_lock = stats_lock or threading.Lock()

    def _is_retryable(self, exc: BaseException) -> bool:
        if isinstance(exc, (BatchCancelledError, KeyboardInterrupt, SystemExit, ValueError)):
            return False

        # Explicit HTTP 400/401/403/404 fast-fail before consulting mocks
        if isinstance(exc, requests.HTTPError) and getattr(exc, "response", None) is not None:
            status = getattr(exc.response, "status_code", None)
            if status in (400, 401, 403, 404):
                return False
            if status in (408, 429, 500, 502, 503, 504):
                return True
        if isinstance(exc, urllib.error.HTTPError):
            status = getattr(exc, "code", None)
            if status in (400, 401, 403, 404):
                return False
            if status in (408, 429, 500, 502, 503, 504):
                return True

        engine_obj = self.translator
        if isinstance(engine_obj, BaseTranslator):
            return engine_obj.is_retryable_error(exc)

        is_retry_fn = getattr(engine_obj, "is_retryable_error", None)
        if callable(is_retry_fn):
            res = is_retry_fn(exc)
            if isinstance(res, bool):
                return res

        freq_fn = getattr(engine_obj, "is_error_request_frequency", None)
        if callable(freq_fn) and freq_fn(exc) is True:
            return True

        return _FALLBACK_CLASSIFIER.is_retryable_error(exc)

    def _invoke_once(self, text: str) -> str:
        # Bypass TextTranslator.translate() (translate.py:60-61 infinite-loop hazard)
        # while preserving its [a-zA-Z] skip and thread-safe call/char accounting.
        if isinstance(self._inner, TextTranslator):
            if not _ALPHA_RE.match(text):
                return text
            result = self._inner.translator.translate(
                text, self._inner.language_to, self._inner.language_from
            )
            with self._stats_lock:
                self._inner.number_of_calls += 1
                self._inner.tot_char += len(text)
            return result

        if isinstance(self._inner, BaseTranslator):
            return self._inner.translate(text, self.language_to, self.language_from)

        return self._inner.translate(text)

    def translate(self, text: str) -> str:
        attempt = 0
        while True:
            if self._cancel_event.is_set():
                raise BatchCancelledError("Batch translation cancelled")
            try:
                return self._invoke_once(text)
            except BaseException as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                if not self._is_retryable(exc) or attempt >= self.max_retries:
                    raise
                delay = self.base_backoff * (2 ** attempt)
                attempt += 1
                if delay > 0:
                    if self._cancel_event.wait(delay):
                        raise BatchCancelledError("Batch translation cancelled during backoff") from exc


def _extract_supports_multithread(obj: Any, engine: str) -> bool:
    if obj is not None:
        if getattr(obj, "supports_multithread", True) is False:
            return False
        inner = getattr(obj, "translator", None)
        if inner is not None and getattr(inner, "supports_multithread", True) is False:
            return False
        return True
    cls = ENGINE_REGISTRY.get(engine.strip().lower())
    if cls is not None and getattr(cls, "supports_multithread", True) is False:
        return False
    return True


def _contiguous_ranges(indices: list[int]) -> list[tuple[int, int]]:
    if not indices:
        return []
    ranges: list[tuple[int, int]] = []
    start = prev = indices[0]
    for idx in indices[1:]:
        if idx == prev + 1:
            prev = idx
        else:
            ranges.append((start, prev))
            start = prev = idx
    ranges.append((start, prev))
    return ranges


class PdfBatchTranslator:
    """Chunked, resumable, thread-safe PDF translation coordinator."""

    def __init__(
        self,
        input_path: str | Path,
        output_path: str | Path,
        work_dir: str | Path,
        *,
        engine: str = "google",
        l_from: str = "en",
        l_to: str = "vi",
        scan_policy: ScanPolicy = "abort",
        glossary: dict[str, str] | None = None,
        min_scale: float = 0.68,
        max_workers: int = 2,
        min_batch_pages: int = 5,
        max_batch_pages: int = 10,
        translator: Any = None,
        translator_factory: Callable[[], Any] | None = None,
        max_retries: int = 4,
        base_backoff: float = 0.5,
        on_progress: Callable[[BatchCheckpoint], None] | None = None,
        job_id: str | None = None,
        run_qa: bool = True,
        qa_sample_pages: int | None = 40,
    ) -> None:
        self.input_path = str(Path(input_path).resolve())
        self.output_path = str(Path(output_path).resolve())
        self.work_dir = Path(work_dir).resolve()
        self.batches_dir = self.work_dir / "batches"
        self.checkpoint_path = self.work_dir / "checkpoint.json"
        self.partial_path = self.work_dir / "partial.pdf"
        self.tm_path = self.work_dir / "pdf_tm.sqlite3"
        self.qa_report_path = self.work_dir / "qa_report.json"

        self.engine = engine
        self.l_from = l_from
        self.l_to = l_to
        self.scan_policy: ScanPolicy = scan_policy
        self.glossary: dict[str, str] = dict(glossary or {})
        self.glossary_hash = compute_glossary_hash(self.glossary)
        self.min_scale = float(min_scale)
        self.max_workers = max(1, int(max_workers))
        self.min_batch_pages = max(1, int(min_batch_pages))
        self.max_batch_pages = max(self.min_batch_pages, int(max_batch_pages))
        self.translator = translator
        self.translator_factory = translator_factory
        self.max_retries = max(0, int(max_retries))
        self.base_backoff = max(0.0, float(base_backoff))
        self.on_progress = on_progress
        self.job_id = job_id or hashlib.sha1(f"{self.input_path}:{time.time()}".encode("utf-8")).hexdigest()[:12]
        self.run_qa = bool(run_qa)
        self.qa_sample_pages = qa_sample_pages

        self._cancel_event = threading.Event()
        self._assembly_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.checkpoint: BatchCheckpoint | None = None

    def _effective_workers(self) -> int:
        if not _extract_supports_multithread(self.translator, self.engine):
            return 1
        return max(1, self.max_workers)

    def cancel(self) -> None:
        """Signal cancellation so running/sleeping batches stop cleanly without corrupting state."""
        self._cancel_event.set()
        with self._assembly_lock:
            if self.checkpoint is not None and self.checkpoint.status in ("pending", "running", "paused"):
                self.checkpoint.status = "cancelled"
                self.checkpoint.updated_at = time.time()
                _atomic_write_json(self.checkpoint_path, self.checkpoint.to_dict())

    def _batch_slice_path(self, batch_idx: int) -> Path:
        return self.batches_dir / f"batch_{batch_idx:04d}.pdf"

    def _write_checkpoint_locked(self) -> None:
        assert self.checkpoint is not None
        self.checkpoint.updated_at = time.time()
        _atomic_write_json(self.checkpoint_path, self.checkpoint.to_dict())

    def _assemble_pdf_from_batches(
        self,
        dest_path: Path,
        batch_indices: list[int],
        batches: list[list[int]],
        cls_map: dict[int, PageClassification],
        toc: list[Any],
        metadata: dict[str, Any],
        *,
        fast: bool = False,
    ) -> list[int]:
        """Assemble `dest_path` atomically from completed `batch_XXXX.pdf` slices.

        Returns the ordered list of original 0-based page indices included in the assembled PDF.
        """
        included_orig_pages: list[int] = []
        slice_files: list[Path] = []
        for b_idx in sorted(batch_indices):
            b_pages = batches[b_idx]
            keep = (
                b_pages
                if self.scan_policy != "skip"
                else [p for p in b_pages if cls_map[p].kind != "SCAN"]
            )
            if not keep:
                continue
            sp = self._batch_slice_path(b_idx)
            if sp.is_file():
                slice_files.append(sp)
                included_orig_pages.extend(keep)

        if not slice_files:
            return []

        merged = pymupdf.open()
        tmp_path = f"{os.path.abspath(str(dest_path))}.tmp_{os.getpid()}_{threading.get_ident()}.pdf"
        os.makedirs(os.path.dirname(os.path.abspath(str(dest_path))) or ".", exist_ok=True)
        try:
            for sp in slice_files:
                with pymupdf.open(str(sp)) as sdoc:
                    merged.insert_pdf(sdoc)

            if toc:
                orig1_to_out1 = {
                    orig_p0 + 1: out_idx + 1
                    for out_idx, orig_p0 in enumerate(included_orig_pages)
                }
                remapped_toc = []
                for entry in toc:
                    lvl, title, p1 = entry[0], entry[1], entry[2]
                    if p1 in orig1_to_out1:
                        remapped_toc.append([lvl, title, orig1_to_out1[p1]])
                remapped_toc = _normalize_toc_levels(remapped_toc)
                try:
                    merged.set_toc(remapped_toc)
                except Exception:
                    pass

            if metadata:
                try:
                    merged.set_metadata(metadata)
                except Exception:
                    pass

            merged.save(tmp_path, garbage=1 if fast else 3, deflate=True)
            merged.close()
            os.replace(tmp_path, str(dest_path))
        finally:
            try:
                if not merged.is_closed:
                    merged.close()
            except Exception:
                pass
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        return included_orig_pages

    def run(self) -> BatchCheckpoint:
        """Classify input PDF, validate scan policy, split into density batches, and execute."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.batches_dir.mkdir(parents=True, exist_ok=True)

        with pymupdf.open(self.input_path) as doc:
            all_cls = classify_pdf_pages(doc)
            toc = doc.get_toc(simple=False)
            metadata = dict(doc.metadata or {})

        cls_map = {c.page_index: c for c in all_cls}
        total_pages = len(all_cls)
        scan_pages = [c.page_index for c in all_cls if c.kind == "SCAN"]

        if scan_pages and self.scan_policy == "abort":
            raise ScanPagesDetectedError(scan_pages)
        if self.scan_policy == "skip" and len(scan_pages) >= total_pages:
            raise ScanPagesDetectedError(scan_pages)

        batches = compute_density_batches(
            list(range(total_pages)),
            all_cls,
            min_batch_pages=self.min_batch_pages,
            max_batch_pages=self.max_batch_pages,
        )

        self.checkpoint = BatchCheckpoint(
            job_id=self.job_id,
            input_path=self.input_path,
            output_path=self.output_path,
            partial_path=str(self.partial_path),
            tm_path=str(self.tm_path),
            engine=self.engine,
            l_from=self.l_from,
            l_to=self.l_to,
            scan_policy=self.scan_policy,
            glossary_hash=self.glossary_hash,
            glossary=dict(self.glossary),
            status="cancelled" if self._cancel_event.is_set() else "running",
            total_pages=total_pages,
            batches=batches,
            completed_batches=[],
            completed_pages=[],
            pending_pages=list(range(total_pages)),
            overflow_pages=[],
            scan_pages=scan_pages,
            error_pages={},
            pages_per_sec=0.0,
            eta_seconds=None,
            elapsed_seconds=0.0,
            updated_at=time.time(),
            min_scale=self.min_scale,
            max_workers=self.max_workers,
            min_batch_pages=self.min_batch_pages,
            max_batch_pages=self.max_batch_pages,
            run_qa=self.run_qa,
            qa_sample_pages=self.qa_sample_pages,
        )
        with self._assembly_lock:
            self._write_checkpoint_locked()

        if self._cancel_event.is_set():
            return self.checkpoint

        return self._execute_batches(list(range(len(batches))), cls_map, toc, metadata)

    @classmethod
    def from_checkpoint(
        cls,
        work_dir: str | Path,
        *,
        translator: Any = None,
        translator_factory: Callable[[], Any] | None = None,
        on_progress: Callable[[BatchCheckpoint], None] | None = None,
        max_workers: int | None = None,
    ) -> PdfBatchTranslator:
        """Reload persisted checkpoint configuration and TM path across process restarts."""
        wdir = Path(work_dir).resolve()
        ckpt_file = wdir / "checkpoint.json"
        if not ckpt_file.is_file():
            raise FileNotFoundError(f"Checkpoint file not found: {ckpt_file}")
        with open(ckpt_file, encoding="utf-8") as f:
            data = json.load(f)
        ckpt = BatchCheckpoint.from_dict(data)
        if not Path(ckpt.input_path).is_file():
            raise FileNotFoundError(f"Original input PDF missing: {ckpt.input_path}")

        instance = cls(
            ckpt.input_path,
            ckpt.output_path,
            wdir,
            engine=ckpt.engine,
            l_from=ckpt.l_from,
            l_to=ckpt.l_to,
            scan_policy=ckpt.scan_policy,  # type: ignore[arg-type]
            glossary=ckpt.glossary,
            min_scale=ckpt.min_scale,
            max_workers=max_workers if max_workers is not None else ckpt.max_workers,
            min_batch_pages=ckpt.min_batch_pages,
            max_batch_pages=ckpt.max_batch_pages,
            translator=translator,
            translator_factory=translator_factory,
            on_progress=on_progress,
            job_id=ckpt.job_id,
            run_qa=ckpt.run_qa,
            qa_sample_pages=ckpt.qa_sample_pages,
        )
        instance.checkpoint = ckpt
        return instance

    def resume(self) -> BatchCheckpoint:
        """Resume from exact next uncompleted batch without re-translating completed batches."""
        if self.checkpoint is None:
            if not self.checkpoint_path.is_file():
                return self.run()
            with open(self.checkpoint_path, encoding="utf-8") as f:
                self.checkpoint = BatchCheckpoint.from_dict(json.load(f))

        self._cancel_event.clear()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.batches_dir.mkdir(parents=True, exist_ok=True)

        if self.checkpoint.status == "completed" and Path(self.output_path).is_file():
            return self.checkpoint

        with pymupdf.open(self.input_path) as doc:
            all_cls = classify_pdf_pages(doc)
            toc = doc.get_toc(simple=False)
            metadata = dict(doc.metadata or {})
        cls_map = {c.page_index: c for c in all_cls}

        batches = self.checkpoint.batches
        verified_completed: list[int] = []
        for b_idx in sorted(set(self.checkpoint.completed_batches)):
            if 0 <= b_idx < len(batches):
                b_pages = batches[b_idx]
                keep = (
                    b_pages
                    if self.scan_policy != "skip"
                    else [p for p in b_pages if cls_map[p].kind != "SCAN"]
                )
                if not keep or self._batch_slice_path(b_idx).is_file():
                    verified_completed.append(b_idx)

        completed_set = set(verified_completed)
        completed_pages = sorted(
            p for b_idx in verified_completed for p in batches[b_idx]
        )
        completed_page_set = set(completed_pages)

        with self._assembly_lock:
            self.checkpoint.completed_batches = verified_completed
            self.checkpoint.completed_pages = completed_pages
            self.checkpoint.pending_pages = [
                p for p in range(self.checkpoint.total_pages) if p not in completed_page_set
            ]
            self.checkpoint.error_pages = {}
            self.checkpoint.status = "running"
            self._write_checkpoint_locked()

        remaining = [i for i in range(len(batches)) if i not in completed_set]
        return self._execute_batches(remaining, cls_map, toc, metadata)

    def _execute_batches(
        self,
        remaining_batch_indices: list[int],
        cls_map: dict[int, PageClassification],
        toc: list[Any],
        metadata: dict[str, Any],
    ) -> BatchCheckpoint:
        assert self.checkpoint is not None
        ckpt = self.checkpoint
        batches = ckpt.batches
        total_pages = ckpt.total_pages
        effective_workers = self._effective_workers()
        prior_elapsed = float(ckpt.elapsed_seconds)
        session_start = time.monotonic()
        session_pages_done = 0

        thread_local = threading.local()

        def _get_worker_proxy() -> _RetryingTranslatorProxy:
            proxy = getattr(thread_local, "proxy", None)
            if proxy is not None:
                return proxy
            if self.translator_factory is not None:
                raw_tr = self.translator_factory()
            elif self.translator is not None:
                raw_tr = self.translator
            else:
                raw_tr = TextTranslator(self.engine, self.l_to, self.l_from)
            proxy = _RetryingTranslatorProxy(
                raw_tr,
                engine=self.engine,
                l_from=self.l_from,
                l_to=self.l_to,
                max_retries=self.max_retries,
                base_backoff=self.base_backoff,
                cancel_event=self._cancel_event,
                stats_lock=self._stats_lock,
            )
            thread_local.proxy = proxy
            return proxy

        tm = SQLiteTranslationMemory(self.tm_path)

        def _tm_lookup(src: str) -> str | None:
            return tm.lookup(src, self.glossary_hash, self.engine, self.l_from, self.l_to)

        def _tm_store(src: str, translated: str) -> None:
            tm.store(src, translated, self.glossary_hash, self.engine, self.l_from, self.l_to)

        def _run_batch(b_idx: int) -> tuple[int, list[int], list[int], list[int]]:
            if self._cancel_event.is_set():
                raise BatchCancelledError("Batch translation cancelled")
            batch_pages = batches[b_idx]
            pages_to_translate = [p for p in batch_pages if cls_map[p].kind != "SCAN"]
            keep_pages = batch_pages if self.scan_policy != "skip" else pages_to_translate
            proxy = _get_worker_proxy()

            with pymupdf.open(self.input_path) as worker_doc:
                _, flagged = translate_pdf_pages_in_doc(
                    worker_doc,
                    pages_to_translate,
                    cls_map,
                    proxy,  # type: ignore[arg-type]
                    glossary=self.glossary or None,
                    min_scale=self.min_scale,
                    cache_lookup=_tm_lookup,
                    cache_store=_tm_store,
                    cancel_check=self._cancel_event.is_set,
                )
                if self._cancel_event.is_set():
                    raise BatchCancelledError("Batch translation cancelled")

                if keep_pages:
                    slice_path = self._batch_slice_path(b_idx)
                    tmp_slice = f"{slice_path}.tmp_{os.getpid()}_{threading.get_ident()}.pdf"
                    batch_doc = pymupdf.open()
                    try:
                        for start_p, end_p in _contiguous_ranges(keep_pages):
                            batch_doc.insert_pdf(worker_doc, from_page=start_p, to_page=end_p)
                        batch_doc.ez_save(tmp_slice)
                        batch_doc.close()
                        os.replace(tmp_slice, str(slice_path))
                    finally:
                        try:
                            if not batch_doc.is_closed:
                                batch_doc.close()
                        except Exception:
                            pass
                        if os.path.exists(tmp_slice):
                            try:
                                os.remove(tmp_slice)
                            except OSError:
                                pass

            try:
                pymupdf.TOOLS.store_shrink(100)
            except Exception:
                pass
            gc.collect()
            return b_idx, batch_pages, keep_pages, flagged

        failed_exc: BaseException | None = None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as pool:
                pending_iter = iter(remaining_batch_indices)
                in_flight: dict[concurrent.futures.Future[tuple[int, list[int], list[int], list[int]]], int] = {}

                def _fill_window() -> None:
                    while (
                        len(in_flight) < effective_workers
                        and not self._cancel_event.is_set()
                        and failed_exc is None
                    ):
                        try:
                            next_idx = next(pending_iter)
                        except StopIteration:
                            break
                        fut = pool.submit(_run_batch, next_idx)
                        in_flight[fut] = next_idx

                _fill_window()
                while in_flight:
                    done, _ = concurrent.futures.wait(
                        in_flight.keys(),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for fut in done:
                        b_idx = in_flight.pop(fut)
                        try:
                            done_idx, batch_pages, _keep_pages, flagged = fut.result()
                        except BatchCancelledError:
                            continue
                        except BaseException as exc:
                            with self._assembly_lock:
                                for pno in batches[b_idx]:
                                    ckpt.error_pages[str(pno)] = f"{type(exc).__name__}: {exc}"
                                ckpt.status = "failed"
                                ckpt.elapsed_seconds = round(
                                    prior_elapsed + (time.monotonic() - session_start), 3
                                )
                                self._write_checkpoint_locked()
                            if failed_exc is None:
                                failed_exc = exc
                            continue

                        ckpt_snapshot: BatchCheckpoint | None = None
                        with self._assembly_lock:
                            if done_idx not in ckpt.completed_batches:
                                ckpt.completed_batches.append(done_idx)
                                ckpt.completed_batches.sort()
                            comp_pages_set = set(ckpt.completed_pages) | set(batch_pages)
                            ckpt.completed_pages = sorted(comp_pages_set)
                            ckpt.pending_pages = [
                                p for p in range(total_pages) if p not in comp_pages_set
                            ]
                            ckpt.overflow_pages = sorted(set(ckpt.overflow_pages) | set(flagged))

                            session_pages_done += len(batch_pages)
                            session_elapsed = max(1e-6, time.monotonic() - session_start)
                            ckpt.elapsed_seconds = round(prior_elapsed + session_elapsed, 3)
                            ckpt.pages_per_sec = round(session_pages_done / session_elapsed, 4)
                            if ckpt.pages_per_sec > 0:
                                ckpt.eta_seconds = round(
                                    len(ckpt.pending_pages) / ckpt.pages_per_sec, 2
                                )
                            else:
                                ckpt.eta_seconds = None

                            partial_stride = 1 if len(batches) <= 20 else max(2, len(batches) // 10)
                            done_count = len(ckpt.completed_batches)
                            if (
                                done_count == 1
                                or done_count % partial_stride == 0
                                or self._cancel_event.is_set()
                            ):
                                self._assemble_pdf_from_batches(
                                    self.partial_path,
                                    ckpt.completed_batches,
                                    batches,
                                    cls_map,
                                    toc,
                                    metadata,
                                    fast=len(batches) > 20,
                                )
                            self._write_checkpoint_locked()
                            if self.on_progress is not None:
                                ckpt_snapshot = copy.deepcopy(ckpt)

                        if ckpt_snapshot is not None and self.on_progress is not None:
                            self.on_progress(ckpt_snapshot)

                    _fill_window()

            if failed_exc is not None:
                raise failed_exc

            if self._cancel_event.is_set() and len(ckpt.completed_batches) < len(batches):
                with self._assembly_lock:
                    if ckpt.completed_batches:
                        self._assemble_pdf_from_batches(
                            self.partial_path,
                            ckpt.completed_batches,
                            batches,
                            cls_map,
                            toc,
                            metadata,
                            fast=len(batches) > 20,
                        )
                    ckpt.status = "cancelled"
                    ckpt.elapsed_seconds = round(
                        prior_elapsed + (time.monotonic() - session_start), 3
                    )
                    self._write_checkpoint_locked()
                return ckpt

            if len(ckpt.completed_batches) == len(batches):
                included_orig_pages = self._assemble_pdf_from_batches(
                    Path(self.output_path),
                    ckpt.completed_batches,
                    batches,
                    cls_map,
                    toc,
                    metadata,
                )
                if not included_orig_pages:
                    raise ScanPagesDetectedError(ckpt.scan_pages)

                if self.run_qa:
                    if (
                        self.qa_sample_pages is not None
                        and len(included_orig_pages) > self.qa_sample_pages
                        and self.qa_sample_pages > 0
                    ):
                        step = len(included_orig_pages) / self.qa_sample_pages
                        checked = sorted(
                            {
                                included_orig_pages[min(len(included_orig_pages) - 1, int(i * step))]
                                for i in range(self.qa_sample_pages)
                            }
                        )
                    else:
                        checked = list(included_orig_pages)

                    page_pairs = (
                        [(orig_p, out_p) for out_p, orig_p in enumerate(included_orig_pages)]
                        if self.scan_policy == "skip"
                        else None
                    )
                    qa_report = verify_translated_pdf(
                        self.input_path,
                        self.output_path,
                        checked_pages=checked,
                        flagged_overflow_pages=ckpt.overflow_pages,
                        expect_vietnamese=(self.l_to.strip().lower() == "vi"),
                        require_page_count_match=(self.scan_policy != "skip"),
                        page_pairs=page_pairs,
                    )
                    save_qa_report_json(qa_report, self.qa_report_path)

                ckpt_snapshot = None
                with self._assembly_lock:
                    ckpt.status = "completed"
                    ckpt.eta_seconds = 0.0
                    ckpt.elapsed_seconds = round(
                        prior_elapsed + (time.monotonic() - session_start), 3
                    )
                    self._write_checkpoint_locked()
                    if self.on_progress is not None:
                        ckpt_snapshot = copy.deepcopy(ckpt)
                if ckpt_snapshot is not None and self.on_progress is not None:
                    self.on_progress(ckpt_snapshot)

            return ckpt
        finally:
            tm.close()
