"""Renders document pages to images for the extraction fallback.

Both fallback targets consume page bitmaps: the remote vision model
(Gemini/OpenAI-compatible) and the local PaddleOCR-VL/PP-OCR engine.
This adapter rasterizes embedded-text/OCR'd PDFs with PyMuPDF at ~150
DPI and passes image documents through unchanged. Failures are
non-fatal by design: the caller proceeds with text-only extraction.
"""

import logging

from app.domain.value_objects.enums import DocumentType

logger = logging.getLogger(__name__)

_ZOOM = 1.5  # 72dpi base * 1.5 ~= 108dpi effective; legible, small payloads
_LOGO = "page_renderer"


def render_page_images(
    content: bytes,
    document_type: DocumentType,
    max_pages: int = 4,
) -> list[bytes]:
    """Return page PNGs (or `[content]` for image docs); never raises."""
    if document_type != DocumentType.PDF:
        return [content] if content else []

    try:
        import pymupdf

        doc = pymupdf.open(stream=content, filetype="pdf")
        try:
            matrix = pymupdf.Matrix(_ZOOM, _ZOOM)
            pages = [
                page.get_pixmap(matrix=matrix).tobytes("png")
                for page in doc.pages(0, min(doc.page_count, max_pages))
            ]
        finally:
            doc.close()
        logger.info(
            "Renderizadas %s pagina(s) a PNG para el extractor de vision",
            len(pages),
            extra={"pages": len(pages), "logger_name": _LOGO},
        )
        return pages
    except Exception as exc:  # corrupted PDF / missing native lib: degrade
        logger.warning(
            "No se pudieron renderizar paginas (%s: %s); el extractor "
            "trabajara solo con texto",
            type(exc).__name__,
            str(exc)[:120],
        )
        return []


__all__ = ["render_page_images"]
