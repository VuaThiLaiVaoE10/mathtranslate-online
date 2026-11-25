"""Mandatory structural & visual QA verifier for layout-preserving PDF translation."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Literal

import pymupdf

from .pdf_doc import (
    ENGLISH_PROSE_WORD_RE,
    LEAKED_PLACEHOLDER_RE,
    MATH_FONT_RE,
    _collect_math_drawing_rects,
    _detect_body_font_size,
    is_math_span,
)

VIETNAMESE_DIACRITIC_RE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
    r"ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ]"
)


@dataclass
class PageVerification:
    page_index: int
    status: Literal["PASS", "WARN", "FAIL"]
    orig_images: int
    out_images: int
    leaked_placeholders: list[str]
    formula_similarity: float
    has_vietnamese_diacritics: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class PdfVerificationReport:
    passed: bool
    orig_pages: int
    out_pages: int
    page_count_match: bool
    toc_preserved: bool
    pages: list[PageVerification]
    rendered_comparisons: list[str] = field(default_factory=list)


def _find_standalone_formula_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Locate standalone display-equation bounding boxes on the original page for visual diffing."""
    raw = page.get_text("dict")
    blocks = raw.get("blocks", [])
    body_size = _detect_body_font_size(blocks)
    drawing_rects = _collect_math_drawing_rects(page)
    rects: list[pymupdf.Rect] = []

    for blk in blocks:
        if blk.get("type") != 0:
            continue
        bbox = pymupdf.Rect(blk["bbox"])
        if bbox.is_empty or bbox.width < 10 or bbox.height < 6:
            continue
        if bbox.y1 < page.rect.height * 0.065 or bbox.y0 > page.rect.height * 0.935:
            continue

        spans = [s for ln in blk.get("lines", []) for s in ln.get("spans", [])]
        math_chars = 0
        non_ws = 0
        prose_spans = []
        for sp in spans:
            t = sp.get("text", "")
            n = len(re.sub(r"\s+", "", t))
            if not n:
                continue
            non_ws += n
            if is_math_span(sp, body_size):
                math_chars += n
            else:
                prose_spans.append(sp)

        if non_ws < 2:
            continue
        prose_words = ENGLISH_PROSE_WORD_RE.findall(" ".join(s.get("text", "") for s in prose_spans))
        has_math_font = any(
            MATH_FONT_RE.search((s.get("font") or "").split("+")[-1]) for s in spans
        )
        overlaps_drw = any(not (bbox & dr).is_empty for dr in drawing_rects)
        if len(prose_words) == 0 and (has_math_font or math_chars >= 2 or overlaps_drw):
            # Inset by 1pt to avoid adjacent paragraph text anti-aliasing fringes
            inner = pymupdf.Rect(bbox.x0 + 1.0, bbox.y0 + 1.0, bbox.x1 - 1.0, bbox.y1 - 1.0)
            if not inner.is_empty and inner.width >= 6 and inner.height >= 4:
                rects.append(inner)
    return rects


def _pixmap_similarity(pix_a: pymupdf.Pixmap, pix_b: pymupdf.Pixmap) -> float:
    """Compute normalized pixel similarity [0.0..1.0] (1.0 - MAE/255) in pure Python."""
    buf_a = memoryview(pix_a.samples)
    buf_b = memoryview(pix_b.samples)
    n = min(len(buf_a), len(buf_b))
    if n == 0:
        return 1.0
    # Sample every byte for small formula crops, stride 4 for huge buffers
    stride = 1 if n <= 200_000 else 4
    diff_sum = sum(abs(buf_a[i] - buf_b[i]) for i in range(0, n, stride))
    count = (n + stride - 1) // stride
    mae = diff_sum / (count * 255.0)
    return round(max(0.0, 1.0 - mae), 4)


def render_side_by_side_png(
    orig_page: pymupdf.Page,
    out_page: pymupdf.Page,
    dest_png_path: str,
    *,
    dpi: int = 150,
) -> str:
    """Render original (left) and translated (right) page into a single comparison PNG."""
    pix_l = orig_page.get_pixmap(dpi=dpi, alpha=False)
    pix_r = out_page.get_pixmap(dpi=dpi, alpha=False)
    gap = 16
    w = pix_l.width + gap + pix_r.width
    h = max(pix_l.height, pix_r.height)

    # Compose side-by-side via a temporary PDF page so zero external PIL dependency is needed
    comp_doc = pymupdf.open()
    try:
        cp = comp_doc.new_page(width=w, height=h)
        cp.draw_rect(pymupdf.Rect(0, 0, w, h), color=(0.9, 0.9, 0.9), fill=(0.92, 0.92, 0.92))
        cp.insert_image(pymupdf.Rect(0, 0, pix_l.width, pix_l.height), pixmap=pix_l)
        cp.insert_image(pymupdf.Rect(pix_l.width + gap, 0, w, pix_r.height), pixmap=pix_r)
        out_pix = cp.get_pixmap(dpi=72, alpha=False)
        os.makedirs(os.path.dirname(os.path.abspath(dest_png_path)) or ".", exist_ok=True)
        out_pix.save(dest_png_path)
    finally:
        comp_doc.close()
    return dest_png_path


def verify_translated_pdf(
    orig_pdf_path: str,
    out_pdf_path: str,
    *,
    checked_pages: list[int] | None = None,
    flagged_overflow_pages: list[int] | None = None,
    render_dir: str | None = None,
    expect_vietnamese: bool = True,
) -> PdfVerificationReport:
    """Run all mandatory QA checks comparing `out_pdf_path` against `orig_pdf_path`."""
    flagged_set = set(flagged_overflow_pages or [])
    orig_doc = pymupdf.open(orig_pdf_path)
    out_doc = pymupdf.open(out_pdf_path)
    try:
        orig_n = len(orig_doc)
        out_n = len(out_doc)
        page_count_match = orig_n == out_n
        toc_preserved = len(out_doc.get_toc()) == len(orig_doc.get_toc())

        indices = checked_pages if checked_pages is not None else list(range(min(orig_n, out_n)))
        page_reports: list[PageVerification] = []
        rendered_paths: list[str] = []

        for pno in indices:
            if pno >= orig_n or pno >= out_n:
                continue
            p_orig = orig_doc[pno]
            p_out = out_doc[pno]
            notes: list[str] = []

            # 1. Page size parity
            if abs(p_orig.rect.width - p_out.rect.width) > 0.5 or abs(p_orig.rect.height - p_out.rect.height) > 0.5:
                notes.append(f"Page size mismatch: {p_orig.rect} vs {p_out.rect}")

            # 2. Image count parity (out_images >= orig_images because inline cropped formulas may add images)
            orig_imgs = len(p_orig.get_images(full=True))
            out_imgs = len(p_out.get_images(full=True))
            if out_imgs < orig_imgs:
                notes.append(f"Lost embedded images: {out_imgs} < {orig_imgs}")

            # 3. Zero leaked placeholders ({v0}, XMATHX_0, etc.) & zero Unicode replacement tofu (\ufffd)
            out_text = p_out.get_text("text")
            leaks = LEAKED_PLACEHOLDER_RE.findall(out_text)
            if leaks:
                notes.append(f"Leaked placeholders: {leaks[:5]}")
            if "\ufffd" in out_text:
                notes.append("Found replacement character U+FFFD in output text")

            # 4. Vietnamese diacritics presence (when page had translatable English prose)
            orig_prose = len(ENGLISH_PROSE_WORD_RE.findall(p_orig.get_text("text")))
            has_vi = bool(VIETNAMESE_DIACRITIC_RE.search(out_text))
            if expect_vietnamese and orig_prose >= 12 and not has_vi:
                notes.append("Expected Vietnamese diacritics on prose page, none found")

            # 5. Visual similarity inside standalone display-formula regions
            eq_rects = _find_standalone_formula_rects(p_orig)
            sims: list[float] = []
            for eq_r in eq_rects[:12]:
                px1 = p_orig.get_pixmap(clip=eq_r, dpi=150, alpha=False)
                px2 = p_out.get_pixmap(clip=eq_r, dpi=150, alpha=False)
                sims.append(_pixmap_similarity(px1, px2))
            formula_sim = round(min(sims), 4) if sims else 1.0
            if formula_sim < 0.94:
                notes.append(f"Formula visual similarity dropped to {formula_sim:.3f}")

            if pno in flagged_set:
                notes.append("Auto-shrunk below min_scale to prevent text truncation (flagged for review)")

            hard_fail = (
                (not page_count_match)
                or (out_imgs < orig_imgs)
                or bool(leaks)
                or ("\ufffd" in out_text)
                or (formula_sim < 0.90)
                or (expect_vietnamese and orig_prose >= 12 and not has_vi)
            )
            if hard_fail:
                status: Literal["PASS", "WARN", "FAIL"] = "FAIL"
            elif notes:
                status = "WARN"
            else:
                status = "PASS"

            page_reports.append(
                PageVerification(
                    page_index=pno,
                    status=status,
                    orig_images=orig_imgs,
                    out_images=out_imgs,
                    leaked_placeholders=leaks,
                    formula_similarity=formula_sim,
                    has_vietnamese_diacritics=has_vi,
                    notes=notes,
                )
            )

            if render_dir is not None:
                png_path = os.path.join(render_dir, f"compare_page_{pno + 1:03d}.png")
                render_side_by_side_png(p_orig, p_out, png_path, dpi=140)
                rendered_paths.append(png_path)

        overall_pass = page_count_match and all(r.status != "FAIL" for r in page_reports)
        return PdfVerificationReport(
            passed=overall_pass,
            orig_pages=orig_n,
            out_pages=out_n,
            page_count_match=page_count_match,
            toc_preserved=toc_preserved,
            pages=page_reports,
            rendered_comparisons=rendered_paths,
        )
    finally:
        orig_doc.close()
        out_doc.close()
