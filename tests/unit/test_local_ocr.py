"""Unit tests for the local OCR/text extraction provider (no Tesseract run).

These verify the *strategy* (embedded text vs rasterize+OCR) using a real,
tiny generated PDF, while stubbing pytesseract itself.
"""

import pytest

from app.domain.exceptions import OCRExtractionError
from app.domain.value_objects.enums import DocumentType
from app.infrastructure.ocr.local_ocr import LocalOCRProvider


@pytest.fixture
def provider():
    return LocalOCRProvider(min_text_chars_per_page=40)


def build_pdf(page_texts: list[str]) -> bytes:
    """Generate a multi-page PDF with the given embedded texts via PyMuPDF."""
    import pymupdf

    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


class TestPdfStrategy:
    def test_embedded_text_used_when_dense_enough(self, provider):
        content = build_pdf(["INVOICE #2026-001 " + "lorem ipsum " * 20])
        result = provider.extract_text(content, DocumentType.PDF)
        assert "embedded-text" in result.method
        assert "INVOICE" in result.text
        assert not result.is_empty

    def test_thin_pages_fall_back_to_tesseract(self, provider, monkeypatch):
        monkeypatch.setattr(
            "app.infrastructure.ocr.local_ocr.LocalOCRProvider._tesseract",
            lambda self, image_bytes: "OCR-RECOVERED-TEXT",
        )
        content = build_pdf(["short"])  # below threshold
        result = provider.extract_text(content, DocumentType.PDF)
        assert "tesseract" in result.method
        assert "OCR-RECOVERED-TEXT" in result.text

    def test_multi_page_merge(self, provider):
        dense = "INVOICE PAGE " + "content " * 30
        content = build_pdf([dense, dense])
        result = provider.extract_text(content, DocumentType.PDF)
        assert result.page_count == 2


class TestErrorHandling:
    def test_garbage_bytes_raise_ocr_error(self, provider):
        with pytest.raises(OCRExtractionError):
            provider.extract_text(b"%PDF-1.4 broken", DocumentType.PDF)

    def test_tesseract_missing_raises_domain_error(self, provider, monkeypatch):
        import pytesseract

        def boom(*args, **kwargs):
            raise pytesseract.TesseractNotFoundError()

        monkeypatch.setattr("pytesseract.image_to_string", boom)
        with pytest.raises(OCRExtractionError):
            provider.extract_text(b"\xff\xd8\xff fake-jpeg", DocumentType.IMAGE)
