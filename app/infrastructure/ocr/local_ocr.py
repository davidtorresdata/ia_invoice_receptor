"""Local OCR/text-extraction provider.

Strategy ("OCR only when necessary"):
  * PDF  -> read embedded text with PyMuPDF; pages whose text layer is too
            thin are rasterized and sent to Tesseract.
  * IMAGE -> straight to Tesseract.

Swapping this for AWS Textract / Azure Document Intelligence / Google
Document AI only requires another `OCRProvider` implementation + factory
entry — nothing else in the system changes.
"""

import io
import logging

import pymupdf
import pytesseract
from PIL import Image

from app.domain.exceptions import OCRExtractionError
from app.domain.services.ocr_provider import OCRProvider, OCRResult
from app.domain.value_objects.enums import DocumentType

logger = logging.getLogger(__name__)

_RASTER_DPI = 200


class LocalOCRProvider(OCRProvider):
    """Embedded-text-first extraction with Tesseract fallback."""

    def __init__(
        self,
        *,
        language: str = "eng+spa",
        min_text_chars_per_page: int = 40,
    ) -> None:
        self._language = language
        self._min_chars = max(1, min_text_chars_per_page)

    # ------------------------------------------------------------------ public
    def extract_text(self, content: bytes, document_type: DocumentType) -> OCRResult:
        try:
            if document_type == DocumentType.PDF:
                return self._extract_pdf(content)
            return self._extract_image(content)
        except OCRExtractionError:
            raise
        except Exception as exc:  # defensive: adapters own library quirks
            logger.exception("Local text extraction failed")
            raise OCRExtractionError(f"Text extraction failed: {exc}") from exc

    # -------------------------------------------------------------------- pdf
    def _extract_pdf(self, content: bytes) -> OCRResult:
        methods: set[str] = set()
        chunks: list[str] = []
        ocr_pages = 0

        with pymupdf.open(stream=content, filetype="pdf") as doc:
            for page in doc:
                embedded = page.get_text("text").strip()
                if len(embedded) >= self._min_chars:
                    methods.add("embedded-text")
                    chunks.append(embedded)
                    continue

                # Scanned page -> rasterize + OCR.
                pixmap = page.get_pixmap(dpi=_RASTER_DPI)
                image_bytes = pixmap.tobytes("png")
                ocr_text = self._tesseract(image_bytes)
                methods.add("tesseract")
                ocr_pages += 1
                chunks.append(ocr_text or embedded)

        method_label = "+".join(sorted(methods)) or "empty"
        if ocr_pages:
            method_label += f" (ocr_pages={ocr_pages})"
        return OCRResult(
            text="\n\n".join(chunks),
            page_count=len(chunks),
            method=method_label,
        )

    # ------------------------------------------------------------------ image
    def _extract_image(self, content: bytes) -> OCRResult:
        text = self._tesseract(content)
        return OCRResult(text=text or "", page_count=1, method="tesseract")

    def _tesseract(self, image_bytes: bytes) -> str:
        try:
            image = Image.open(io.BytesIO(image_bytes))
            return pytesseract.image_to_string(image, lang=self._language).strip()
        except pytesseract.TesseractNotFoundError as exc:
            raise OCRExtractionError(
                "Tesseract binary not found. Install tesseract-ocr in the image/host."
            ) from exc
        except Exception as exc:
            raise OCRExtractionError(f"Tesseract failed: {exc}") from exc
