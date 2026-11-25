"""Stage PDF-M1 automated tests: page classification, MathType PUA shield, layout rebuild, and QA verifier."""

from __future__ import annotations

from pathlib import Path
import re

import pymupdf
import pytest

from mathtranslate.pdf_doc import (
    ScanPagesDetectedError,
    classify_pdf_pages,
    has_non_unicode_glyph,
    is_math_char,
    translate_pdf_file,
)
from mathtranslate.pdf_qa import verify_translated_pdf

REPO_ROOT = Path(__file__).resolve().parents[2]
REF_PAPER_PDF = REPO_ROOT / "reference" / "example" / "paper.pdf"
LINEAR_PDF = REPO_ROOT / "pdf2zh_files" / "linear.pdf"


class OfflineViMockTranslator:
    """Deterministic EN->VI mock translator with realistic Vietnamese expansion & diacritics."""

    def __init__(self, engine: str = "mock", language_to: str = "vi", language_from: str = "en") -> None:
        self.engine = engine
        self.language_to = language_to
        self.language_from = language_from
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        self.calls.append(text)
        # Ensure zero raw formula symbols ever enter the translator
        assert "\uf0b1" not in text, "MathType PUA symbol leaked into translator!"
        out = text
        replacements = [
            ("This Letter examines", "Bài báo này khảo sát chi tiết"),
            ("equations", "các phương trình toán học"),
            ("the", "các"),
            ("and", "và"),
            ("is", "là"),
            ("for", "cho"),
            ("with", "với"),
            ("in", "trong"),
        ]
        for k, v in replacements:
            out = re.sub(rf"\b{k}\b", v, out, flags=re.IGNORECASE)
        if out == text:
            out = f"Bản dịch tiếng Việt đầy đủ dấu của: {out}"
        return out


def _build_scan_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # Create a large raster image covering >70% of the page with zero text layer
    img_doc = pymupdf.open()
    ip = img_doc.new_page(width=400, height=600)
    ip.draw_rect(pymupdf.Rect(0, 0, 400, 600), color=(0.2, 0.2, 0.2), fill=(0.85, 0.85, 0.85))
    pix = ip.get_pixmap(dpi=72)
    img_doc.close()
    page.insert_image(pymupdf.Rect(40, 40, 555, 790), pixmap=pix)
    doc.save(str(path))
    doc.close()


def _build_mathtype_and_links_pdf(path: Path) -> None:
    """Create a synthetic PDF containing prose, a MathType PUA symbol (U+F0B1), TOC, link, and display math."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)

    # Prose block with inline MathType Private Use Area char (\uf0b1 = ± in Symbol PUA)
    page.insert_text((72, 100), "Theorem 1: Every matrix has an eigenvalue in this vector space.", fontsize=11)
    page.insert_text((72, 140), "Inline formula with PUA symbol: \uf0b1 and citation [12] in the proof.", fontsize=11)

    # Standalone display formula block + fraction bar vector line
    page.insert_text((240, 210), "\u222b f(x) dx = \u2211 \u03b1_i \u00d7 \u03b2_i", fontsize=12)
    page.draw_line((238, 215), (360, 215), color=(0, 0, 0), width=0.8)

    # Embedded figure image
    fig_doc = pymupdf.open()
    fp = fig_doc.new_page(width=80, height=50)
    fp.draw_rect(pymupdf.Rect(0, 0, 80, 50), color=(0.1, 0.3, 0.6), fill=(0.2, 0.4, 0.7))
    pix = fp.get_pixmap(dpi=72)
    fig_doc.close()
    page.insert_image(pymupdf.Rect(200, 280, 360, 360), pixmap=pix)

    # Hyperlink + Bookmark TOC + Metadata
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 90, 200, 108), "uri": "https://arxiv.org"})
    doc.set_toc([[1, "Section 1: Eigenvalues", 1]])
    doc.set_metadata({"title": "MathType Test Document", "author": "UnitTest"})
    doc.save(str(path))
    doc.close()


def test_mathtype_pua_and_glyph_detectors():
    assert is_math_char("\uf0b1") is True  # Word/MathType Symbol PUA
    assert is_math_char("\u222b") is True  # Integral
    assert is_math_char("A") is False
    assert has_non_unicode_glyph("ABCDEF+MT-Extra", "a") is True
    assert has_non_unicode_glyph("SymbolMT", "x") is True
    assert has_non_unicode_glyph("EuclidSymbol", "x") is True
    assert has_non_unicode_glyph("AdvP4C4E74", "½") is True
    assert has_non_unicode_glyph("TimesNewRomanPSMT", "\uf0b1") is True
    assert has_non_unicode_glyph("TimesNewRomanPSMT", "Hello") is False


def test_scan_page_aborts_without_ocr_and_keep_original_works(tmp_path):
    scan_pdf = tmp_path / "scan.pdf"
    out_pdf = tmp_path / "scan_out.pdf"
    _build_scan_pdf(scan_pdf)

    with pymupdf.open(str(scan_pdf)) as d:
        cls = classify_pdf_pages(d)
    assert len(cls) == 1
    assert cls[0].kind == "SCAN"

    with pytest.raises(ScanPagesDetectedError) as exc_info:
        translate_pdf_file(str(scan_pdf), str(out_pdf), translator=OfflineViMockTranslator())
    assert "KHÔNG tự ý chạy OCR" in str(exc_info.value)
    assert not out_pdf.exists()

    # With explicit keep_original policy, scan page is preserved intact
    res = translate_pdf_file(
        str(scan_pdf),
        str(out_pdf),
        scan_policy="keep_original",
        translator=OfflineViMockTranslator(),
    )
    assert res.total_pages == 1
    assert out_pdf.exists()


def test_synthetic_mathtype_pua_links_toc_and_qa_verifier(tmp_path):
    src_pdf = tmp_path / "mathtype_src.pdf"
    out_pdf = tmp_path / "mathtype_vi.pdf"
    render_dir = tmp_path / "renders"
    _build_mathtype_and_links_pdf(src_pdf)

    mock_tr = OfflineViMockTranslator()
    res = translate_pdf_file(str(src_pdf), str(out_pdf), translator=mock_tr)

    assert res.total_pages == 1
    report = verify_translated_pdf(
        str(src_pdf),
        str(out_pdf),
        flagged_overflow_pages=res.flagged_overflow_pages,
        render_dir=str(render_dir),
    )
    assert report.passed is True
    assert report.page_count_match is True
    assert report.toc_preserved is True
    assert report.pages[0].formula_similarity >= 0.98
    assert report.pages[0].leaked_placeholders == []
    assert len(report.rendered_comparisons) == 1
    assert Path(report.rendered_comparisons[0]).is_file()

    with pymupdf.open(str(out_pdf)) as out_doc:
        assert out_doc.metadata["title"] == "MathType Test Document"
        assert len(out_doc[0].get_links()) == 1
        out_txt = re.sub(r"\s+", " ", out_doc[0].get_text("text")).lower()
        assert "[12]" in out_txt
        assert "giá trị riêng" in out_txt


def test_overflow_auto_shrink_never_truncates_text(tmp_path):
    src_pdf = tmp_path / "tight_box.pdf"
    out_pdf = tmp_path / "tight_box_vi.pdf"

    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    page.insert_textbox(
        pymupdf.Rect(50, 50, 260, 82),
        "Short English sentence inside a tight box.",
        fontsize=10,
    )
    doc.save(str(src_pdf))
    doc.close()

    class VerboseViTranslator(OfflineViMockTranslator):
        def translate(self, text: str) -> str:
            return (
                "Đây là một câu dịch tiếng Việt dài hơn rất nhiều so với câu gốc tiếng Anh "
                "để kiểm tra cơ chế tự thu nhỏ cỡ chữ và gắn cờ trang tràn khung mà không bao giờ cắt chữ KẾT_THÚC."
            )

    res = translate_pdf_file(str(src_pdf), str(out_pdf), min_scale=0.85, translator=VerboseViTranslator())
    assert 0 in res.flagged_overflow_pages

    with pymupdf.open(str(out_pdf)) as out_doc:
        out_txt = out_doc[0].get_text("text")
        assert "KẾT_THÚC" in out_txt


def test_real_paper_pdf_5pages_and_linear_sample_pages(tmp_path):
    assert REF_PAPER_PDF.is_file()
    assert LINEAR_PDF.is_file()

    # 1. Classify all 386 pages of linear.pdf quickly
    with pymupdf.open(str(LINEAR_PDF)) as lin_doc:
        lin_cls = classify_pdf_pages(lin_doc)
    assert len(lin_cls) == 386

    # 2. Translate all 5 pages of reference/example/paper.pdf with mock translator & verify QA
    out_paper = tmp_path / "paper_vi.pdf"
    res = translate_pdf_file(str(REF_PAPER_PDF), str(out_paper), translator=OfflineViMockTranslator())
    assert res.total_pages == 5

    qa = verify_translated_pdf(
        str(REF_PAPER_PDF),
        str(out_paper),
        flagged_overflow_pages=res.flagged_overflow_pages,
    )
    assert qa.passed is True
    assert qa.orig_pages == 5 and qa.out_pages == 5
    for p_rep in qa.pages:
        assert p_rep.leaked_placeholders == []
        assert p_rep.out_images >= p_rep.orig_images
        assert p_rep.formula_similarity >= 0.95
