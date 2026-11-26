"""Layout-preserving EN -> VI PDF translation pipeline (Stage PDF-M1).

Capabilities:
1. Pre-flight page classification: TEXT | SCAN | MIXED (never runs OCR automatically).
2. Multi-column layout analysis & reading-order sorting.
3. Three-layer Formula Shield (MathType / TeX / Publisher fonts, PUA 0xE000..0xF8FF,
   Unicode math operators, sub/superscripts, and vector math drawings).
4. Glossary-consistent & neighbor-context-aware natural text translation.
5. Surgical content-stream redaction (preserving 100% of images, vector paths, and
   standalone equations) + embedded Unicode Vietnamese fonts (Times New Roman / Arial /
   Consolas) + inline 300-DPI formula crops + overflow auto-scaling & page flagging.
6. Preserves page count, page dimensions, links, TOC/bookmarks, and metadata.
"""

from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures
from dataclasses import dataclass, field
import html
import json
import os
from pathlib import Path
import re
import sys
import threading
from typing import Callable, Literal

import pymupdf

from .translate import TextTranslator

PageKind = Literal["TEXT", "SCAN", "MIXED"]
ScanPolicy = Literal["abort", "keep_original", "skip"]

# Layer 1: Math, MathType, TeX, and Publisher custom symbol fonts (case-insensitive)
MATH_FONT_RE = re.compile(
    r"(?i)(?:^|[^a-z])("
    r"cmmi|cmsy|cmex|cmmib|msam|msbm|euex|eufm|eurm|eusb|euclid|"
    r"mt[-_ ]?extra|mtextra|symbol|wingdings|webdings|"
    r"cambria\s*math|latin\s*modern\s*math|stix\s*math|asana\s*math|xits\s*math|"
    r"fira\s*math|dejavu\s*math|libertinus\s*math|garamond[-_ ]?math|"
    r"wasy|stmary|rsfs|txsy|pxsy|fourier|yhmath|mathematica|"
    r"advp4c|advtt|advot.*\+|advotf5c2"
    r")"
)

# Custom non-Unicode fonts whose character codes do NOT correspond to standard Unicode glyphs
NON_UNICODE_GLYPH_FONT_RE = re.compile(
    r"(?i)(?:^|[^a-z])("
    r"mt[-_ ]?extra|mtextra|symbol|wingdings|webdings|"
    r"cmex|msam|msbm|wasy|stmary|rsfs|"
    r"advp4c|advtt.*\+|advot.*\+|euclid\s*(?:extra|symbol|math)"
    r")"
)

MONO_FONT_RE = re.compile(r"(?i)(mono|consol|courier|menlo|fira\s*code|jetbrains|typewriter|cmtt)")
SANS_FONT_RE = re.compile(r"(?i)(arial|helvetica|calibri|segoe|verdana|tahoma|roboto|inter|sans|cmss)")
BOLD_FONT_RE = re.compile(r"(?i)(bold|black|heavy|\.b\b|-b\b|cmbx)")
ITALIC_FONT_RE = re.compile(r"(?i)(italic|oblique|\.i\b|-i\b|cmti|cmsl)")

# Citations, equation numbers, DOIs, URLs, units/numbers to keep intact inside prose
PROTECTED_INLINE_RE = re.compile(
    r"(?:"
    r"https?://\S+|"
    r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+|"
    r"\[(?:\d+(?:\s*[–\-−,]\s*\d+)*)\]|"
    r"\((?:\d+(?:\.\d+)+|[IVX]+)\)"
    r")"
)

ENGLISH_PROSE_WORD_RE = re.compile(r"[A-Za-z]{3,}")
PLACEHOLDER_RE = re.compile(r"XMATHX(?:[_\s-]*(\d+))+", re.IGNORECASE)
LEAKED_PLACEHOLDER_RE = re.compile(r"(?:XMATHX|\{\s*v\s*\d+\s*\})", re.IGNORECASE)

DEFAULT_EN_VI_GLOSSARY: dict[str, str] = {
    "eigenvalue": "giá trị riêng",
    "eigenvalues": "các giá trị riêng",
    "eigenvector": "vectơ riêng",
    "eigenvectors": "các vectơ riêng",
    "linear transformation": "ánh xạ tuyến tính",
    "vector space": "không gian vectơ",
    "subspace": "không gian con",
    "basis": "cơ sở",
    "determinant": "định thức",
    "matrix": "ma trận",
    "matrices": "các ma trận",
    "theorem": "định lý",
    "lemma": "bổ đề",
    "corollary": "hệ quả",
    "proposition": "mệnh đề",
    "quantum field theory": "lý thuyết trường lượng tử",
    "spacetime": "không-thời gian",
    "boundary condition": "điều kiện biên",
    "boundary conditions": "các điều kiện biên",
}

AMBIGUOUS_MATH_TERMS: dict[str, list[str]] = {
    "field": ["trường (đại số/vật lý)", "miền", "lĩnh vực"],
    "ring": ["vành (đại số)", "vòng"],
    "kernel": ["hạt nhân (hạch)", "nhân"],
    "rank": ["hạng (ma trận)", "bậc"],
    "manifold": ["đa tạp", "ống góp"],
    "trace": ["vết (ma trận/toán tử)", "vết (đường đi)", "theo dấu"],
    "order": ["bậc (phương trình/nhóm)", "thứ tự", "cấp"],
    "degree": ["bậc (đa thức/đỉnh)", "độ (góc/nhiệt độ)"],
    "spectrum": ["phổ (toán tử/ma trận)", "quang phổ"],
    "norm": ["chuẩn (vectơ/không gian)", "định mức"],
    "normal": ["pháp tuyến", "chuẩn tắc (nhóm con/phân phối)", "thông thường"],
    "projection": ["phép chiếu", "hình chiếu", "dự phóng"],
    "domain": ["miền xác định", "miền (đại số)", "lĩnh vực"],
    "measure": ["độ đo", "phép đo", "biện pháp"],
    "image": ["ảnh (ánh xạ)", "hình ảnh"],
}
_AMBIGUOUS_TERMS_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in AMBIGUOUS_MATH_TERMS) + r")\b"
)


class ScanPagesDetectedError(RuntimeError):
    """Raised when SCAN pages are found and scan_policy == 'abort'."""

    def __init__(self, scan_pages: list[int]) -> None:
        self.scan_pages = scan_pages
        pages_1based = ", ".join(str(p + 1) for p in scan_pages[:25])
        more = f" (+{len(scan_pages) - 25} trang khác)" if len(scan_pages) > 25 else ""
        msg = (
            f"Phát hiện {len(scan_pages)} trang ảnh quét (SCAN) tại trang: {pages_1based}{more}. "
            "Theo quy tắc an toàn, hệ thống KHÔNG tự ý chạy OCR. "
            "Phương án đề xuất: (1) truyền --scan-policy keep_original để giữ nguyên trang scan gốc, "
            "(2) truyền --scan-policy skip, hoặc (3) tiền xử lý OCR chuyên dụng trước khi dịch."
        )
        super().__init__(msg)


@dataclass
class PageClassification:
    page_index: int
    kind: PageKind
    text_chars: int
    image_count: int
    image_area_ratio: float


@dataclass
class InlineToken:
    placeholder: str
    kind: Literal["text", "image"]
    html_fragment: str
    png_name: str | None = None
    png_bytes: bytes | None = None


@dataclass
class ProseBlockPlan:
    block_index: int
    bbox: pymupdf.Rect
    redact_rects: list[pymupdf.Rect]
    masked_text: str
    tokens: list[InlineToken]
    font_family: Literal["serif", "sans", "mono"]
    font_size: float
    is_bold: bool
    is_italic: bool
    text_color_hex: str
    align: str


@dataclass
class PageTranslationStats:
    page_index: int
    kind: PageKind
    translated_blocks: int = 0
    preserved_formula_blocks: int = 0
    inline_formulas_protected: int = 0
    overflow_blocks: int = 0
    min_scale_used: float = 1.0


@dataclass
class PdfTranslationResult:
    input_path: str
    output_path: str
    total_pages: int
    page_classifications: list[PageClassification]
    page_stats: list[PageTranslationStats]
    flagged_overflow_pages: list[int] = field(default_factory=list)
    ambiguous_terms_found: dict[str, list[str]] = field(default_factory=dict)


def is_math_char(ch: str) -> bool:
    code = ord(ch)
    if code < 0x20 and ch not in ("\t", "\n", "\r"):
        return True
    # Private Use Area (includes Word/MathType Symbol PUA 0xF000..0xF0FF)
    if 0xE000 <= code <= 0xF8FF or code >= 0xF0000:
        return True
    # Greek & Coptic
    if 0x0370 <= code <= 0x03FF:
        return True
    # Superscripts/Subscripts, Arrows, Mathematical Operators, Misc Technical, Geometric, Supp Math
    if 0x2070 <= code <= 0x209F:
        return True
    if 0x2190 <= code <= 0x23FF:
        return True
    if 0x25A0 <= code <= 0x27FF:
        return True
    if 0x2900 <= code <= 0x2BFF:
        return True
    # Mathematical Alphanumeric Symbols (U+1D400..U+1D7FF)
    if 0x1D400 <= code <= 0x1D7FF:
        return True
    if ch in "=<>±×÷∓∞∂∇∑∏∫∮√≈≠≤≥≡⊂⊃⊆⊇∈∉∪∩∧∨∀∃⇒⇔→←↔":
        return True
    return False


def has_non_unicode_glyph(font_name: str, text: str) -> bool:
    clean_font = (font_name or "").split("+")[-1]
    if NON_UNICODE_GLYPH_FONT_RE.search(clean_font):
        return True
    for ch in text:
        code = ord(ch)
        if (code < 0x20 and ch not in ("\t", "\n", "\r")) or (0xE000 <= code <= 0xF8FF):
            return True
    return False


def is_math_span(span: dict, body_size: float) -> bool:
    text = span.get("text", "")
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if stripped in (".", ",", ";", ":"):
        return False
    font_name = (span.get("font") or "").split("+")[-1]
    if MATH_FONT_RE.search(font_name):
        return True
    if any(is_math_char(ch) for ch in stripped):
        return True
    size = float(span.get("size") or body_size)
    # Subscript / superscript numbers or single-letter math variables
    if size < body_size * 0.80 and len(stripped) <= 6 and not ENGLISH_PROSE_WORD_RE.search(stripped):
        return True
    if len(stripped) == 1 and stripped.isalpha() and ITALIC_FONT_RE.search(font_name) and stripped not in ("a", "A", "I"):
        return True
    return False


def classify_pdf_pages(doc: pymupdf.Document) -> list[PageClassification]:
    """Classify every page in `doc` as TEXT, SCAN, or MIXED without running OCR."""
    results: list[PageClassification] = []
    for idx in range(len(doc)):
        page = doc[idx]
        page_area = max(1.0, abs(page.rect.width * page.rect.height))
        text_chars = len(re.sub(r"\s+", "", page.get_text("text")))
        img_infos = page.get_image_info()
        img_area = 0.0
        for info in img_infos:
            r = pymupdf.Rect(info.get("bbox", (0, 0, 0, 0))) & page.rect
            if not r.is_empty:
                img_area += r.width * r.height

        img_count = len(page.get_images(full=True))
        ratio = min(1.0, img_area / page_area)
        if text_chars < 20 and (ratio >= 0.45 or (img_count >= 1 and text_chars == 0)):
            kind: PageKind = "SCAN"
        elif text_chars >= 20 and ratio >= 0.40:
            kind = "MIXED"
        else:
            kind = "TEXT"
        results.append(
            PageClassification(
                page_index=idx,
                kind=kind,
                text_chars=text_chars,
                image_count=img_count,
                image_area_ratio=round(ratio, 4),
            )
        )
    return results


def extract_ambiguous_terms(
    doc: pymupdf.Document,
    page_indices: list[int] | None = None,
    *,
    scan_pages_all: bool = False,
) -> dict[str, list[str]]:
    """Detect recurring domain terms that have multiple Vietnamese translations."""
    counts: Counter[str] = Counter()
    if page_indices is not None:
        indices = [idx for idx in page_indices if 0 <= idx < len(doc)]
    elif scan_pages_all:
        indices = list(range(len(doc)))
    else:
        indices = list(range(min(len(doc), 30)))

    for idx in indices:
        txt = doc[idx].get_text("text").lower()
        for m in _AMBIGUOUS_TERMS_RE.finditer(txt):
            counts[m.group(1)] += 1
    return {term: AMBIGUOUS_MATH_TERMS[term] for term, cnt in counts.items() if cnt >= 2}


def _detect_body_font_size(blocks: list[dict]) -> float:
    size_weights: Counter[float] = Counter()
    for blk in blocks:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                txt = (sp.get("text") or "").strip()
                if len(txt) >= 3 and ENGLISH_PROSE_WORD_RE.search(txt):
                    sz = round(float(sp.get("size") or 10.0), 1)
                    size_weights[sz] += len(txt)
    if not size_weights:
        return 10.0
    return size_weights.most_common(1)[0][0]


def _collect_math_drawing_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Collect tight vector drawing rects likely belonging to fraction bars, radicals, or matrices."""
    pw, ph = page.rect.width, page.rect.height
    rects: list[pymupdf.Rect] = []
    for drw in page.get_drawings():
        r = pymupdf.Rect(drw.get("rect", (0, 0, 0, 0)))
        if r.is_empty:
            continue
        # Ignore full-page frames, column rules, or huge background boxes
        if r.width > pw * 0.75 or r.height > ph * 0.35:
            continue
        if r.width <= 180 and r.height <= 90:
            rects.append(r)
    return rects


def _sort_blocks_reading_order(blocks: list[dict], page_width: float) -> list[tuple[int, dict]]:
    """Sort text blocks in natural reading order, handling 1-column and 2-column academic layouts."""
    mid_x = page_width * 0.5
    text_blocks = [(i, b) for i, b in enumerate(blocks) if b.get("type") == 0]

    left_cnt = sum(1 for _, b in text_blocks if b["bbox"][2] <= mid_x + 18 and (b["bbox"][2] - b["bbox"][0]) < page_width * 0.48)
    right_cnt = sum(1 for _, b in text_blocks if b["bbox"][0] >= mid_x - 18 and (b["bbox"][2] - b["bbox"][0]) < page_width * 0.48)
    is_two_col = left_cnt >= 2 and right_cnt >= 2

    if not is_two_col:
        return sorted(text_blocks, key=lambda item: (round(item[1]["bbox"][1], 1), round(item[1]["bbox"][0], 1)))

    # Group into vertical bands separated by full-width spanning blocks (titles, abstracts, wide figures)
    spanning = sorted(
        [(i, b) for i, b in text_blocks if b["bbox"][0] < mid_x - 25 and b["bbox"][2] > mid_x + 25],
        key=lambda item: item[1]["bbox"][1],
    )
    non_spanning = [(i, b) for i, b in text_blocks if not (b["bbox"][0] < mid_x - 25 and b["bbox"][2] > mid_x + 25)]

    ordered: list[tuple[int, dict]] = []
    last_y = -1e9
    for sp_idx, sp_blk in spanning:
        sp_top = sp_blk["bbox"][1]
        band = [(i, b) for i, b in non_spanning if last_y <= b["bbox"][1] < sp_top]
        left_b = sorted([x for x in band if (x[1]["bbox"][0] + x[1]["bbox"][2]) * 0.5 <= mid_x], key=lambda x: x[1]["bbox"][1])
        right_b = sorted([x for x in band if (x[1]["bbox"][0] + x[1]["bbox"][2]) * 0.5 > mid_x], key=lambda x: x[1]["bbox"][1])
        ordered.extend(left_b + right_b)
        ordered.append((sp_idx, sp_blk))
        last_y = sp_top

    tail = [(i, b) for i, b in non_spanning if b["bbox"][1] >= last_y and (i, b) not in ordered]
    left_t = sorted([x for x in tail if (x[1]["bbox"][0] + x[1]["bbox"][2]) * 0.5 <= mid_x], key=lambda x: x[1]["bbox"][1])
    right_t = sorted([x for x in tail if (x[1]["bbox"][0] + x[1]["bbox"][2]) * 0.5 > mid_x], key=lambda x: x[1]["bbox"][1])
    ordered.extend(left_t + right_t)
    return ordered


def _is_running_header_or_footer(bbox: pymupdf.Rect, raw_text: str, page_rect: pymupdf.Rect) -> bool:
    stripped = raw_text.strip()
    if not stripped:
        return True
    # Pure page number at top/bottom margin
    if (bbox.y1 < page_rect.height * 0.065 or bbox.y0 > page_rect.height * 0.935) and len(stripped) <= 90:
        if stripped.isdigit() or re.match(r"^(?:page\s*)?\d+(?:\s*/\s*\d+)?$", stripped, re.IGNORECASE):
            return True
        if len(ENGLISH_PROSE_WORD_RE.findall(stripped)) <= 3:
            return True
    return False


def _color_int_to_hex(color_int: int) -> str:
    if not isinstance(color_int, int):
        return "#000000"
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


def _detect_block_style(spans: list[dict], body_size: float) -> tuple[Literal["serif", "sans", "mono"], float, bool, bool, str]:
    font_counter: Counter[str] = Counter()
    size_counter: Counter[float] = Counter()
    color_counter: Counter[int] = Counter()
    bold_chars = 0
    italic_chars = 0
    total_chars = 0

    for sp in spans:
        txt = (sp.get("text") or "").strip()
        if not txt:
            continue
        n = len(txt)
        total_chars += n
        fname = (sp.get("font") or "").split("+")[-1]
        font_counter[fname] += n
        size_counter[round(float(sp.get("size") or body_size), 1)] += n
        color_counter[int(sp.get("color") or 0)] += n
        flags = int(sp.get("flags") or 0)
        if (flags & 16) or BOLD_FONT_RE.search(fname):
            bold_chars += n
        if (flags & 2) or ITALIC_FONT_RE.search(fname):
            italic_chars += n

    dom_font = font_counter.most_common(1)[0][0] if font_counter else "Times"
    if MONO_FONT_RE.search(dom_font):
        family: Literal["serif", "sans", "mono"] = "mono"
    elif SANS_FONT_RE.search(dom_font):
        family = "sans"
    else:
        family = "serif"

    dom_size = size_counter.most_common(1)[0][0] if size_counter else body_size
    dom_color = _color_int_to_hex(color_counter.most_common(1)[0][0] if color_counter else 0)
    is_bold = total_chars > 0 and (bold_chars / total_chars) >= 0.55
    is_italic = total_chars > 0 and (italic_chars / total_chars) >= 0.55
    return family, dom_size, is_bold, is_italic, dom_color


def _merge_overlapping_inline_blocks(blocks: list[dict]) -> list[dict]:
    """Merge split inline-equation sub-blocks whose bbox overlaps an enclosing prose block."""
    text_blks = [dict(b, lines=[dict(ln, spans=list(ln.get("spans", []))) for ln in b.get("lines", [])]) for b in blocks if b.get("type") == 0]
    other_blks = [b for b in blocks if b.get("type") != 0]

    changed = True
    while changed:
        changed = False
        for i in range(len(text_blks)):
            ri = pymupdf.Rect(text_blks[i]["bbox"])
            if ri.is_empty:
                continue
            for j in range(i + 1, len(text_blks)):
                rj = pymupdf.Rect(text_blks[j]["bbox"])
                if rj.is_empty:
                    continue
                inter = ri & rj
                same_col = abs(ri.x0 - rj.x0) <= 35 or (ri.x0 <= rj.x0 and rj.x1 <= ri.x1 + 8) or (rj.x0 <= ri.x0 and ri.x1 <= rj.x1 + 8)
                min_area = max(1.0, min(ri.get_area(), rj.get_area()))
                # Also catch 1-line inline math/word fragments touching (<2pt gap) a multi-line paragraph block
                v_gap = max(0.0, max(ri.y0, rj.y0) - min(ri.y1, rj.y1))
                short_frag = min(ri.height, rj.height) <= 15.0 and max(ri.height, rj.height) >= 24.0 and v_gap <= 2.0
                if same_col and ((not inter.is_empty and (inter.get_area() / min_area) >= 0.12) or short_frag):
                    combined_lines = text_blks[i]["lines"] + text_blks[j]["lines"]
                    # Coalesce lines that share the same visual baseline band
                    combined_lines.sort(key=lambda ln: ((ln["bbox"][1] + ln["bbox"][3]) * 0.5, ln["bbox"][0]))
                    merged_lines: list[dict] = []
                    for ln in combined_lines:
                        ymid = (ln["bbox"][1] + ln["bbox"][3]) * 0.5
                        if merged_lines:
                            prev = merged_lines[-1]
                            pymid = (prev["bbox"][1] + prev["bbox"][3]) * 0.5
                            if abs(ymid - pymid) <= 5.0:
                                prev["spans"] = sorted(prev["spans"] + ln["spans"], key=lambda s: s["bbox"][0])
                                pb = pymupdf.Rect(prev["bbox"]) | pymupdf.Rect(ln["bbox"])
                                prev["bbox"] = (pb.x0, pb.y0, pb.x1, pb.y1)
                                continue
                        merged_lines.append(ln)
                    ub = ri | rj
                    text_blks[i]["bbox"] = (ub.x0, ub.y0, ub.x1, ub.y1)
                    text_blks[i]["lines"] = merged_lines
                    del text_blks[j]
                    changed = True
                    break
            if changed:
                break

    return text_blks + other_blks


def _plan_page_blocks(
    page: pymupdf.Page,
) -> tuple[list[ProseBlockPlan], int, int]:
    """Analyze a page into prose blocks to translate while shielding standalone & inline formulas."""
    raw = page.get_text("dict")
    blocks = _merge_overlapping_inline_blocks(raw.get("blocks", []))
    body_size = _detect_body_font_size(blocks)
    drawing_rects = _collect_math_drawing_rects(page)
    ordered_blocks = _sort_blocks_reading_order(blocks, page.rect.width)

    plans: list[ProseBlockPlan] = []
    preserved_formula_blocks = 0
    inline_formulas_total = 0

    for blk_idx, blk in ordered_blocks:
        bbox = pymupdf.Rect(blk["bbox"])
        if bbox.is_empty or bbox.width < 8 or bbox.height < 5:
            continue

        all_spans: list[dict] = []
        line_texts: list[str] = []
        for ln in blk.get("lines", []):
            ln_spans = ln.get("spans", [])
            all_spans.extend(ln_spans)
            line_texts.append("".join(s.get("text", "") for s in ln_spans))
        full_raw_text = "\n".join(line_texts).strip()
        if not full_raw_text:
            continue

        if _is_running_header_or_footer(bbox, full_raw_text, page.rect):
            continue

        # Pure URL/DOI/citation/equation-label block -> keep untouched
        if PROTECTED_INLINE_RE.fullmatch(full_raw_text) or full_raw_text.upper().startswith("DOI:"):
            preserved_formula_blocks += 1
            continue

        # Compute math vs prose character balance
        math_chars = 0
        non_ws_chars = 0
        prose_spans: list[dict] = []
        for sp in all_spans:
            t = sp.get("text", "")
            n = len(re.sub(r"\s+", "", t))
            if n == 0:
                continue
            non_ws_chars += n
            if is_math_span(sp, body_size):
                math_chars += n
            else:
                prose_spans.append(sp)

        if non_ws_chars == 0:
            continue

        prose_words = ENGLISH_PROSE_WORD_RE.findall(
            " ".join(s.get("text", "") for s in prose_spans)
        )
        overlaps_math_drawing = any(
            not (bbox & dr).is_empty and ((bbox & dr).get_area() / max(1.0, dr.get_area())) >= 0.5
            for dr in drawing_rects
            if dr.width < bbox.width * 0.9
        )

        # Standalone display equation / symbol-heavy block -> DO NOT REDACT OR TRANSLATE
        if (
            len(prose_words) == 0
            or (math_chars / non_ws_chars) >= 0.42
            or (len(prose_words) <= 2 and math_chars >= 2)
            or (overlaps_math_drawing and len(prose_words) <= 4)
        ):
            preserved_formula_blocks += 1
            continue

        # Prose block! Build masked text while protecting inline math spans & inline citations
        tokens: list[InlineToken] = []
        redact_rects: list[pymupdf.Rect] = []
        parts: list[str] = []

        # Group consecutive math spans on the same line so multi-span inline formulas stay one unit
        for ln_i, ln in enumerate(blk.get("lines", [])):
            if ln_i > 0:
                # Join hyphenated line wraps cleanly
                if parts and parts[-1].endswith("-") and len(parts[-1]) >= 2 and parts[-1][-2].isalpha():
                    parts[-1] = parts[-1][:-1]
                else:
                    parts.append(" ")

            ln_spans = ln.get("spans", [])
            idx = 0
            while idx < len(ln_spans):
                sp = ln_spans[idx]
                txt = sp.get("text", "")
                if not txt:
                    idx += 1
                    continue

                if is_math_span(sp, body_size):
                    # Merge adjacent math spans (and tiny connecting punctuation/digits/spaces)
                    grp_spans = [sp]
                    j = idx + 1
                    while j < len(ln_spans):
                        nxt = ln_spans[j]
                        ntxt = nxt.get("text", "")
                        if is_math_span(nxt, body_size) or (
                            ntxt.strip() in ("", "=", "+", "-", ",", "(", ")", "[", "]", "/", "0", "1", "2", "3", "4")
                            and j + 1 < len(ln_spans)
                            and is_math_span(ln_spans[j + 1], body_size)
                        ):
                            grp_spans.append(nxt)
                            j += 1
                        else:
                            break

                    grp_rect = pymupdf.Rect(grp_spans[0]["bbox"])
                    for gs in grp_spans[1:]:
                        grp_rect |= pymupdf.Rect(gs["bbox"])
                    redact_rects.append(grp_rect)

                    grp_text = "".join(gs.get("text", "") for gs in grp_spans)
                    needs_crop = any(
                        has_non_unicode_glyph(gs.get("font", ""), gs.get("text", ""))
                        or float(gs.get("size") or body_size) < body_size * 0.82
                        for gs in grp_spans
                    )
                    tok_idx = len(tokens)
                    ph = f"XMATHX_{tok_idx}"
                    if needs_crop:
                        clip_r = pymupdf.Rect(
                            max(0.0, grp_rect.x0),
                            max(0.0, grp_rect.y0 - 0.3),
                            min(page.rect.width, grp_rect.x1),
                            min(page.rect.height, grp_rect.y1 + 0.3),
                        )
                        pix = page.get_pixmap(clip=clip_r, dpi=300, alpha=False)
                        png_name = f"p{page.number}_b{blk_idx}_f{tok_idx}.png"
                        h_pt = max(6.0, round(clip_r.height, 1))
                        tokens.append(
                            InlineToken(
                                placeholder=ph,
                                kind="image",
                                html_fragment=f'<img src="{png_name}" height="{h_pt}">',
                                png_name=png_name,
                                png_bytes=pix.tobytes("png"),
                            )
                        )
                    else:
                        clean_html = f"<i>{html.escape(grp_text.strip())}</i>"
                        tokens.append(
                            InlineToken(placeholder=ph, kind="text", html_fragment=clean_html)
                        )
                    parts.append(f" {ph} ")
                    inline_formulas_total += 1
                    idx = j
                else:
                    redact_rects.append(pymupdf.Rect(sp["bbox"]))
                    # Also shield inline citations [12], equation numbers (2.1), URLs/DOIs
                    def _mask_inline_ref(m: re.Match) -> str:
                        val = m.group(0)
                        t_idx = len(tokens)
                        t_ph = f"XMATHX_{t_idx}"
                        tokens.append(
                            InlineToken(
                                placeholder=t_ph,
                                kind="text",
                                html_fragment=html.escape(val),
                            )
                        )
                        return t_ph

                    parts.append(PROTECTED_INLINE_RE.sub(_mask_inline_ref, txt))
                    idx += 1

        masked_text = re.sub(r"[ \t]+", " ", "".join(parts)).strip()
        if not ENGLISH_PROSE_WORD_RE.search(re.sub(r"XMATHX_\d+", "", masked_text)):
            preserved_formula_blocks += 1
            continue

        family, dom_size, is_bold, is_italic, dom_color = _detect_block_style(prose_spans, body_size)
        align = "justify" if len(masked_text) > 120 and bbox.width > page.rect.width * 0.35 else "left"

        # Coalesce adjacent same-line span rects (e.g. TOC dot-leaders) so apply_redactions stays O(lines).
        # Cluster into y-bands first, then sort each band purely by x0 so small y0 jitter cannot reorder
        # right-before-left spans and bridge across >24pt horizontal gaps.
        y_bands: list[tuple[float, float, list[pymupdf.Rect]]] = []
        for rr in sorted(redact_rects, key=lambda r: (r.y0, r.x0)):
            placed = False
            for idx_b in range(len(y_bands) - 1, -1, -1):
                by0, by1, b_rects = y_bands[idx_b]
                if rr.y0 - by0 > 3.0:
                    break
                if abs(by0 - rr.y0) <= 3.0 and abs(by1 - rr.y1) <= 3.0:
                    b_rects.append(rr)
                    placed = True
                    break
            if not placed:
                y_bands.append((rr.y0, rr.y1, [rr]))

        coalesced_rects: list[pymupdf.Rect] = []
        for _, _, b_rects in y_bands:
            band_coalesced: list[pymupdf.Rect] = []
            for rr in sorted(b_rects, key=lambda r: (r.x0, r.x1)):
                if (
                    band_coalesced
                    and abs(band_coalesced[-1].y0 - rr.y0) <= 3.0
                    and abs(band_coalesced[-1].y1 - rr.y1) <= 3.0
                    and rr.x0 - band_coalesced[-1].x1 <= 24.0
                    and band_coalesced[-1].x0 - rr.x1 <= 24.0
                ):
                    band_coalesced[-1] = band_coalesced[-1] | rr
                else:
                    band_coalesced.append(pymupdf.Rect(rr))
            coalesced_rects.extend(band_coalesced)

        plans.append(
            ProseBlockPlan(
                block_index=blk_idx,
                bbox=bbox,
                redact_rects=coalesced_rects,
                masked_text=masked_text,
                tokens=tokens,
                font_family=family,
                font_size=dom_size,
                is_bold=is_bold,
                is_italic=is_italic,
                text_color_hex=dom_color,
                align=align,
            )
        )

    return plans, preserved_formula_blocks, inline_formulas_total


def _merge_glossary(glossary: dict[str, str] | None) -> tuple[dict[str, str], set[str]]:
    """Merge DEFAULT_EN_VI_GLOSSARY with user overrides case-insensitively."""
    merged: dict[str, str] = {
        k.strip(): v.strip()
        for k, v in DEFAULT_EN_VI_GLOSSARY.items()
        if k.strip() and v.strip()
    }
    user_keys_lower: set[str] = set()
    if glossary:
        for raw_k, raw_v in glossary.items():
            k = str(raw_k).strip()
            v = str(raw_v).strip()
            if not k:
                continue
            for existing_k in [ek for ek in merged if ek.lower() == k.lower()]:
                del merged[existing_k]
            if v:
                merged[k] = v
                user_keys_lower.add(k.lower())
    return merged, user_keys_lower


def _extract_edge_prose_snippet(
    page: pymupdf.Page,
    edge: Literal["head", "tail"],
    max_chars: int = 180,
) -> str:
    """Extract a lightweight (<1 ms) leading/trailing prose snippet from `page` without Pixmap crops."""
    candidates: list[tuple[float, float, str]] = []
    for blk in page.get_text("blocks"):
        if len(blk) < 7 or blk[6] != 0:
            continue
        raw_txt = (blk[4] or "").strip()
        if not raw_txt:
            continue
        bbox = pymupdf.Rect(blk[:4])
        if _is_running_header_or_footer(bbox, raw_txt, page.rect):
            continue
        if len(ENGLISH_PROSE_WORD_RE.findall(raw_txt)) < 2:
            continue
        cleaned = re.sub(r"\s+", " ", raw_txt).strip()
        candidates.append((round(float(blk[1]), 1), round(float(blk[0]), 1), cleaned))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]))
    chosen = candidates[0][2] if edge == "head" else candidates[-1][2]
    if len(chosen) <= max_chars:
        return chosen
    return chosen[:max_chars] if edge == "head" else chosen[-max_chars:]


_LLM_TRANSLATE_ONLY_RE = re.compile(
    r"(?:"
    r"(?P<open>[*_]{2,3}|`{1,3}|[*_](?=[^\s\]]))[^\S\r\n]*\[Translate ONLY this paragraph\]"
    r"(?:"
    r"(?:[*_`]{1,3})(?=\s|:|$)[^\S\r\n]*:?"
    r"|"
    r"(?P=open)[^\S\r\n]*:?"
    r"|"
    r"[^\S\r\n]+(?:[*_`]{1,3})(?=\s|:|$)[^\S\r\n]*:?"
    r"|"
    r"[^\S\r\n]*:(?:[*_`]{1,3}(?=\s|$)|(?P=open)|[^\S\r\n]+[*_`]{1,3}(?=\s|$))"
    r")"
    r"|"
    r"(?P<bopen>[*_])[^\S\r\n]+\[Translate ONLY this paragraph\][^\S\r\n]+(?P=bopen)(?=\s|:|$)[^\S\r\n]*:?"
    r"|"
    r"\[Translate ONLY this paragraph\][^\S\r\n]*:?"
    r")(?:\s*:(?=\s|$))?\s*",
    flags=re.IGNORECASE,
)
_LLM_PROMPT_HEADER_LINE_RE = re.compile(
    r"^\s*(?:(?:[-*#+>]+|\d+[.)])[^\S\r\n]+)*(?:(?:[-*#+>]+|\d+[.)])[^\S\r\n]*)?"
    r"(?:"
    r"(?P<open>[*_]{1,3}|`{1,3})[^\S\r\n]*"
    r"(?:"
    r"Glossary[^\S\r\n]*(?::|(?P=open)[^\S\r\n]*:)"
    r"|"
    r"\[Context (?:before|after)[^\]]*\]"
    r")"
    r"|"
    r"(?:Glossary[^\S\r\n]*:|\[Context (?:before|after)[^\]]*\])"
    r")",
    flags=re.IGNORECASE,
)


def _strip_llm_prompt_leaks(raw_out: str) -> str:
    """Strip echoed context/glossary/instruction headers (including markdown-wrapped) from LLM outputs."""
    text = raw_out.strip()
    last_match = None
    for match in _LLM_TRANSLATE_ONLY_RE.finditer(text):
        last_match = match
    if last_match is not None:
        text = text[last_match.end():].strip()
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if _LLM_PROMPT_HEADER_LINE_RE.match(line):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _normalize_toc_levels(entries: list[list]) -> list[list]:
    """Ensure TOC levels start at 1 and never jump by more than +1."""
    normalized: list[list] = []
    prev_lvl = 0
    for entry in entries:
        raw_lvl = int(entry[0]) if entry[0] is not None else 1
        lvl = 1 if not normalized else max(1, min(raw_lvl, prev_lvl + 1))
        prev_lvl = lvl
        normalized.append([lvl, *entry[1:]])
    return normalized


def _apply_glossary_post(text: str, glossary: dict[str, str] | None) -> str:
    if not glossary:
        return text
    # If any English glossary term remained untranslated, replace it cleanly
    out = text
    for src_term, tgt_term in sorted(glossary.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"\b{re.escape(src_term)}\b", tgt_term, out, flags=re.IGNORECASE)
    return out


def _translate_masked_blocks(
    plans: list[ProseBlockPlan],
    translator: TextTranslator,
    glossary: dict[str, str] | None = None,
    cache_lookup: Callable[[str], str | None] | None = None,
    cache_store: Callable[[str, str], None] | None = None,
    *,
    prev_page_tail: str = "",
    next_page_head: str = "",
) -> list[str]:
    """Translate prose blocks with neighbor context (for LLMs) and strict placeholder recovery."""
    is_llm = translator.engine in ("gemini", "openai", "ollama")
    merged_glossary, user_keys_lower = _merge_glossary(glossary)
    ordered_glossary = sorted(
        merged_glossary.items(),
        key=lambda kv: (0 if kv[0].lower() in user_keys_lower else 1, -len(kv[0]), kv[0].lower()),
    )

    def _postprocess(plan: ProseBlockPlan, src: str, raw_out: str) -> str:
        cleaned = _apply_glossary_post(raw_out, merged_glossary)

        def _norm_ph(m: re.Match) -> str:
            digits = re.findall(r"\d+", m.group(0))
            return f"XMATHX_{digits[0]}" if digits else m.group(0)

        cleaned = PLACEHOLDER_RE.sub(_norm_ph, cleaned)
        for tok in plan.tokens:
            if tok.placeholder not in cleaned:
                cleaned = f"{cleaned} {tok.placeholder}"
        if cache_store is not None:
            cache_store(src, cleaned)
        return cleaned

    def _translate_one(idx_plan: tuple[int, ProseBlockPlan]) -> str:
        i, plan = idx_plan
        src = re.sub(r"\bFIG\.\s*", "Figure ", plan.masked_text)
        if cache_lookup is not None:
            cached = cache_lookup(src)
            if cached is not None:
                return cached

        if is_llm:
            prev_raw = plans[i - 1].masked_text if i > 0 else prev_page_tail
            next_raw = plans[i + 1].masked_text if i + 1 < len(plans) else next_page_head
            prev_ctx = re.sub(r"\s+", " ", prev_raw).strip()[-180:]
            next_ctx = re.sub(r"\s+", " ", next_raw).strip()[:180]
            glossary_hits = [
                f"{k} = {v}"
                for k, v in ordered_glossary
                if re.search(rf"\b{re.escape(k)}\b", src, flags=re.IGNORECASE)
            ]
            ctx_header = []
            if glossary_hits:
                ctx_header.append("Glossary: " + "; ".join(glossary_hits[:24]))
            if prev_ctx:
                ctx_header.append(f"[Context before - do NOT output]: {prev_ctx}")
            if next_ctx:
                ctx_header.append(f"[Context after - do NOT output]: {next_ctx}")
            if ctx_header:
                prompt_text = "\n".join(ctx_header) + f"\n[Translate ONLY this paragraph]:\n{src}"
                raw_out = _strip_llm_prompt_leaks(translator.translate(prompt_text))
            else:
                raw_out = _strip_llm_prompt_leaks(translator.translate(src))
        else:
            raw_out = translator.translate(src)

        return _postprocess(plan, src, raw_out)

    # For NMT engines (google/deepl/tencent), batch multiple blocks per HTTP call (~15x fewer requests, avoids HTTP 429)
    if not is_llm and len(plans) > 1:
        outputs: list[str | None] = [None] * len(plans)
        pending: list[tuple[int, ProseBlockPlan, str]] = []
        for i, plan in enumerate(plans):
            src = re.sub(r"\bFIG\.\s*", "Figure ", plan.masked_text)
            if cache_lookup is not None:
                cached = cache_lookup(src)
                if cached is not None:
                    outputs[i] = cached
                    continue
            pending.append((i, plan, src))

        sep = "\n\nXMATHXSEP\n\n"
        sep_split_re = re.compile(r"\s*XMATHXSEP\s*", re.IGNORECASE)
        cur_batch: list[tuple[int, ProseBlockPlan, str]] = []
        cur_len = 0
        batches: list[list[tuple[int, ProseBlockPlan, str]]] = []
        for item in pending:
            if cur_batch and cur_len + len(item[2]) + len(sep) > 2200:
                batches.append(cur_batch)
                cur_batch = []
                cur_len = 0
            cur_batch.append(item)
            cur_len += len(item[2]) + len(sep)
        if cur_batch:
            batches.append(cur_batch)

        for batch in batches:
            if len(batch) == 1:
                idx0, plan0, _ = batch[0]
                outputs[idx0] = _translate_one((idx0, plan0))
                continue
            joined = sep.join(x[2] for x in batch)
            raw_batch = translator.translate(joined)
            pieces = sep_split_re.split(raw_batch)
            if len(pieces) == len(batch):
                for (b_idx, b_plan, b_src), piece in zip(batch, pieces):
                    outputs[b_idx] = _postprocess(b_plan, b_src, piece.strip())
            else:
                for b_idx, b_plan, _ in batch:
                    outputs[b_idx] = _translate_one((b_idx, b_plan))

        return [o or "" for o in outputs]

    return [_translate_one((i, p)) for i, p in enumerate(plans)]


_FONT_CACHE: tuple[dict[str, bytes], str] | None = None
_FONT_CACHE_LOCK = threading.Lock()


def _build_font_archive_and_css() -> tuple[pymupdf.Archive, str]:
    """Create a lightweight PyMuPDF font archive with cached Vietnamese TrueType fonts."""
    global _FONT_CACHE
    with _FONT_CACHE_LOCK:
        if _FONT_CACHE is None:
            win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
            font_map: dict[str, bytes] = {}
            if win_fonts.is_dir():
                for fname in (
                    "times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf",
                    "arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf",
                    "consola.ttf",
                ):
                    fpath = win_fonts / fname
                    if fpath.is_file():
                        font_map[fname] = fpath.read_bytes()
                css = """
                @font-face { font-family: MTSerif; src: url(times.ttf); }
                @font-face { font-family: MTSerif; src: url(timesbd.ttf); font-weight: bold; }
                @font-face { font-family: MTSerif; src: url(timesi.ttf); font-style: italic; }
                @font-face { font-family: MTSerif; src: url(timesbi.ttf); font-weight: bold; font-style: italic; }
                @font-face { font-family: MTSans; src: url(arial.ttf); }
                @font-face { font-family: MTSans; src: url(arialbd.ttf); font-weight: bold; }
                @font-face { font-family: MTSans; src: url(ariali.ttf); font-style: italic; }
                @font-face { font-family: MTSans; src: url(arialbi.ttf); font-weight: bold; font-style: italic; }
                @font-face { font-family: MTMono; src: url(consola.ttf); }
                """
            else:
                for d in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF", "/Library/Fonts"):
                    if os.path.isdir(d):
                        for fname in ("DejaVuSerif.ttf", "DejaVuSans.ttf", "DejaVuSansMono.ttf"):
                            fp = Path(d) / fname
                            if fp.is_file():
                                font_map[fname] = fp.read_bytes()
                        break
                css = """
                @font-face { font-family: MTSerif; src: url(DejaVuSerif.ttf); }
                @font-face { font-family: MTSans; src: url(DejaVuSans.ttf); }
                @font-face { font-family: MTMono; src: url(DejaVuSansMono.ttf); }
                """
            _FONT_CACHE = (font_map, css)

        font_bytes, css = _FONT_CACHE

    arch = pymupdf.Archive()
    for fname, data in font_bytes.items():
        arch.add(data, fname)
    return arch, css


def _render_plan_to_html(plan: ProseBlockPlan, translated_text: str) -> str:
    """Convert translated text + protected XMATHX tokens into safe HTML for insert_htmlbox."""
    token_map = {t.placeholder: t for t in plan.tokens}
    segments = re.split(r"(XMATHX_\d+)", translated_text)
    html_parts: list[str] = []
    for seg in segments:
        if seg in token_map:
            html_parts.append(token_map[seg].html_fragment)
        else:
            # Strip any stray unknown placeholder so nothing like XMATHX_999 or {v0} ever leaks
            safe_seg = LEAKED_PLACEHOLDER_RE.sub("", seg)
            html_parts.append(html.escape(safe_seg))

    inner = "".join(html_parts).strip()
    if plan.is_bold:
        inner = f"<b>{inner}</b>"
    if plan.is_italic:
        inner = f"<i>{inner}</i>"
    return inner


def translate_pdf_pages_in_doc(
    doc: pymupdf.Document,
    page_indices: list[int],
    classifications: dict[int, PageClassification],
    translator: TextTranslator,
    *,
    glossary: dict[str, str] | None = None,
    min_scale: float = 0.68,
    cache_lookup: Callable[[str], str | None] | None = None,
    cache_store: Callable[[str, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[PageTranslationStats], list[int]]:
    """Translate `page_indices` in-place inside `doc`, preserving vector math, images, links, and TOC."""
    base_arch, font_css = _build_font_archive_and_css()
    stats_list: list[PageTranslationStats] = []
    flagged_pages: list[int] = []
    is_llm = getattr(translator, "engine", "") in ("gemini", "openai", "ollama")
    last_processed_pno = -2
    last_english_tail = ""

    for pno in page_indices:
        if cancel_check is not None and cancel_check():
            break

        cls_info = classifications[pno]
        if cls_info.kind == "SCAN":
            stats_list.append(PageTranslationStats(page_index=pno, kind="SCAN"))
            last_processed_pno = pno
            last_english_tail = ""
            continue

        page = doc[pno]
        # Backup links on this page before redaction so internal/URI links are never lost
        saved_links = page.get_links()

        prev_page_tail = ""
        next_page_head = ""
        current_english_tail = ""
        if is_llm:
            if pno > 0:
                prev_cls = classifications.get(pno - 1)
                if prev_cls is None or prev_cls.kind != "SCAN":
                    if last_processed_pno == pno - 1:
                        prev_page_tail = last_english_tail
                    else:
                        prev_page_tail = _extract_edge_prose_snippet(doc[pno - 1], "tail")
            if pno + 1 < len(doc):
                next_cls = classifications.get(pno + 1)
                if next_cls is None or next_cls.kind != "SCAN":
                    next_page_head = _extract_edge_prose_snippet(doc[pno + 1], "head")
            current_english_tail = _extract_edge_prose_snippet(page, "tail")

        plans, preserved_cnt, inline_cnt = _plan_page_blocks(page)
        if not plans:
            stats_list.append(
                PageTranslationStats(
                    page_index=pno,
                    kind=cls_info.kind,
                    preserved_formula_blocks=preserved_cnt,
                )
            )
            last_processed_pno = pno
            last_english_tail = current_english_tail
            continue

        if is_llm and not current_english_tail:
            current_english_tail = re.sub(r"\s+", " ", plans[-1].masked_text).strip()[-180:]

        translated_texts = _translate_masked_blocks(
            plans,
            translator,
            glossary=glossary,
            cache_lookup=cache_lookup,
            cache_store=cache_store,
            prev_page_tail=prev_page_tail,
            next_page_head=next_page_head,
        )
        last_processed_pno = pno
        last_english_tail = current_english_tail

        # Register any inline formula PNG crops into a page-scoped archive
        page_arch, _ = _build_font_archive_and_css()
        for plan in plans:
            for tok in plan.tokens:
                if tok.kind == "image" and tok.png_name and tok.png_bytes:
                    page_arch.add(tok.png_bytes, tok.png_name)

        # Step 5a: Transparent text-only redaction on ONLY the prose spans
        for plan in plans:
            for r in plan.redact_rects:
                # Slight inward vertical trim (0.3pt) avoids clipping adjacent display-math integral tips
                safe_r = pymupdf.Rect(r.x0, r.y0 + 0.25, r.x1, max(r.y0 + 1.0, r.y1 - 0.25))
                page.add_redact_annot(safe_r, fill=False)

        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )

        # Restore links if redaction removed any overlapping link annotations
        existing_link_rects = {tuple(round(v, 1) for v in lnk.get("from", (0, 0, 0, 0))) for lnk in page.get_links()}
        for lnk in saved_links:
            key = tuple(round(v, 1) for v in lnk.get("from", (0, 0, 0, 0)))
            if key not in existing_link_rects:
                try:
                    page.insert_link(lnk)
                except Exception:
                    pass

        # Step 5b: Typeset Vietnamese HTML into original bounding boxes with auto-fit & overflow guard
        overflow_cnt = 0
        min_scale_seen = 1.0
        all_occupied = [pymupdf.Rect(b["bbox"]) for b in page.get_text("dict").get("blocks", [])] + [
            p.bbox for p in plans
        ]

        for plan, vi_text in zip(plans, translated_texts):
            fam_map = {"serif": "MTSerif", "sans": "MTSans", "mono": "MTMono"}
            css_family = fam_map[plan.font_family]
            block_css = (
                f"{font_css}\n"
                f"body {{ font-family: {css_family}; font-size: {plan.font_size:.2f}pt; "
                f"line-height: 1.12; color: {plan.text_color_hex}; text-align: {plan.align}; margin: 0; padding: 0; }}\n"
                f"img {{ vertical-align: -15%; }}"
            )
            html_body = _render_plan_to_html(plan, vi_text)

            # Give 1.5pt breathing room on right/bottom so exact-height single lines don't falsely wrap
            target_rect = pymupdf.Rect(
                plan.bbox.x0,
                max(0.0, plan.bbox.y0 - 0.8),
                min(page.rect.width - 6.0, plan.bbox.x1 + 2.0),
                min(page.rect.height - 4.0, plan.bbox.y1 + 2.5),
            )

            spare_h, used_scale = page.insert_htmlbox(
                target_rect,
                html_body,
                css=block_css,
                archive=page_arch,
                scale_low=min_scale,
            )

            if spare_h < 0:
                # Never truncate text! Try expanding downward into empty whitespace if no block sits immediately below
                overflow_cnt += 1
                max_bottom = page.rect.height - 8.0
                for other in all_occupied:
                    if other is plan.bbox or (other & plan.bbox).get_area() > 0.5 * plan.bbox.get_area():
                        continue
                    horiz_overlap = min(target_rect.x1, other.x1) - max(target_rect.x0, other.x0)
                    if horiz_overlap > 4.0 and other.y0 >= plan.bbox.y1 - 1.0:
                        max_bottom = min(max_bottom, other.y0 - 1.0)

                expanded_rect = pymupdf.Rect(
                    target_rect.x0,
                    target_rect.y0,
                    min(page.rect.width - 4.0, target_rect.x1 + 8.0),
                    max(target_rect.y1, max_bottom),
                )
                # scale_low=0 guarantees PyMuPDF shrinks until 100% of words fit (ZERO character truncation!)
                _, used_scale = page.insert_htmlbox(
                    expanded_rect,
                    html_body,
                    css=block_css,
                    archive=page_arch,
                    scale_low=0,
                )

            if used_scale > 0:
                min_scale_seen = min(min_scale_seen, float(used_scale))

        if overflow_cnt > 0:
            flagged_pages.append(pno)

        stats_list.append(
            PageTranslationStats(
                page_index=pno,
                kind=cls_info.kind,
                translated_blocks=len(plans),
                preserved_formula_blocks=preserved_cnt,
                inline_formulas_protected=inline_cnt,
                overflow_blocks=overflow_cnt,
                min_scale_used=round(min_scale_seen, 3),
            )
        )

    return stats_list, flagged_pages


def translate_pdf_file(
    input_path: str,
    output_path: str,
    *,
    engine: str = "google",
    l_from: str = "en",
    l_to: str = "vi",
    pages: list[int] | None = None,
    scan_policy: ScanPolicy = "abort",
    glossary: dict[str, str] | None = None,
    min_scale: float = 0.68,
    translator: TextTranslator | None = None,
    cache_lookup: Callable[[str], str | None] | None = None,
    cache_store: Callable[[str, str], None] | None = None,
) -> PdfTranslationResult:
    """Translate an entire PDF (or selected 0-based `pages`) preserving layout, math, links, and TOC."""
    src_doc = pymupdf.open(input_path)
    try:
        toc = src_doc.get_toc(simple=False)
        metadata = dict(src_doc.metadata or {})
        all_cls = classify_pdf_pages(src_doc)
        cls_map = {c.page_index: c for c in all_cls}

        if pages is None:
            target_pages = list(range(len(src_doc)))
        else:
            target_pages = sorted(set(pages))
            invalid = [p for p in target_pages if p < 0 or p >= len(src_doc)]
            if invalid:
                raise ValueError(
                    f"Page indices out of range [0, {len(src_doc) - 1}]: {invalid}"
                )

        scan_in_targets = [p for p in target_pages if cls_map[p].kind == "SCAN"]
        if scan_in_targets and scan_policy == "abort":
            raise ScanPagesDetectedError(scan_in_targets)

        pages_to_run = [p for p in target_pages if cls_map[p].kind != "SCAN"]
        if scan_policy == "skip" and scan_in_targets and len(src_doc) - len(scan_in_targets) <= 0:
            raise ScanPagesDetectedError(scan_in_targets)

        ambiguous = extract_ambiguous_terms(src_doc, target_pages)
        active_translator = translator if translator is not None else TextTranslator(engine, l_to, l_from)

        stats, flagged = translate_pdf_pages_in_doc(
            src_doc,
            pages_to_run,
            cls_map,
            active_translator,
            glossary=glossary,
            min_scale=min_scale,
            cache_lookup=cache_lookup,
            cache_store=cache_store,
        )

        if scan_policy == "skip" and scan_in_targets:
            deleted_set = set(scan_in_targets)
            old_to_new_1based: dict[int, int] = {}
            new_idx_1b = 1
            for orig_idx in range(len(src_doc)):
                if orig_idx not in deleted_set:
                    old_to_new_1based[orig_idx + 1] = new_idx_1b
                    new_idx_1b += 1
            src_doc.delete_pages(sorted(deleted_set))
            if toc:
                remapped_toc = []
                for entry in toc:
                    lvl, title, p1 = entry[0], entry[1], entry[2]
                    if p1 in old_to_new_1based:
                        remapped_toc.append([lvl, title, old_to_new_1based[p1]])
                remapped_toc = _normalize_toc_levels(remapped_toc)
                try:
                    src_doc.set_toc(remapped_toc)
                except Exception:
                    pass
        elif toc:
            # Preserve TOC/bookmarks and PDF metadata
            try:
                src_doc.set_toc(toc)
            except Exception:
                pass
        if metadata:
            src_doc.set_metadata(metadata)

        # Atomic write (.tmp -> os.replace) with subset font garbage collection
        out_abs = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
        tmp_path = f"{out_abs}.tmp_{os.getpid()}_{threading.get_ident()}.pdf"
        try:
            src_doc.ez_save(tmp_path)
            os.replace(tmp_path, out_abs)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        return PdfTranslationResult(
            input_path=input_path,
            output_path=out_abs,
            total_pages=len(src_doc),
            page_classifications=all_cls,
            page_stats=stats,
            flagged_overflow_pages=flagged,
            ambiguous_terms_found=ambiguous,
        )
    finally:
        src_doc.close()


def _parse_pages_arg(pages_str: str | None, total_pages: int) -> list[int] | None:
    if not pages_str:
        return None
    selected: list[int] = []
    for part in pages_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = max(1, int(a))
            end = min(total_pages, int(b))
            selected.extend(range(start - 1, end))
        else:
            p = int(part)
            if 1 <= p <= total_pages:
                selected.append(p - 1)
    return sorted(set(selected))


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Translate PDF (EN -> VI) preserving layout and math formulas.")
    parser.add_argument("input_pdf", help="Path to input PDF")
    parser.add_argument("-o", "--output", required=True, help="Path to output translated PDF")
    parser.add_argument("-engine", default="google", help="Translation engine (google, gemini, openai, ollama, deepl, tencent)")
    parser.add_argument("-from", dest="l_from", default="en", help="Source language code (default: en)")
    parser.add_argument("-to", dest="l_to", default="vi", help="Target language code (default: vi)")
    parser.add_argument("--pages", default=None, help="Optional 1-based page range, e.g. '1-5,10'")
    parser.add_argument("--scan-policy", choices=["abort", "keep_original", "skip"], default="abort")
    parser.add_argument("--glossary", default=None, help="Optional path to JSON glossary file")
    opts = parser.parse_args(args)

    glossary_data = None
    if opts.glossary:
        with open(opts.glossary, encoding="utf-8") as f:
            glossary_data = json.load(f)

    with pymupdf.open(opts.input_pdf) as probe:
        total = len(probe)
    selected_pages = _parse_pages_arg(opts.pages, total)

    res = translate_pdf_file(
        opts.input_pdf,
        opts.output,
        engine=opts.engine,
        l_from=opts.l_from,
        l_to=opts.l_to,
        pages=selected_pages,
        scan_policy=opts.scan_policy,
        glossary=glossary_data,
    )
    print(f"Translated PDF saved to: {res.output_path} ({res.total_pages} pages)")
    if res.flagged_overflow_pages:
        flagged_1b = ", ".join(str(p + 1) for p in res.flagged_overflow_pages)
        print(f"Pages auto-shrunk below min_scale (review layout): {flagged_1b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
