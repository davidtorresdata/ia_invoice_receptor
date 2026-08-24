"""OCR engines for the local execution mode.

The local branch of the LLM_EXECUTION switch runs PP-OCR (PaddleOCR)
over rendered page images and feeds the recognized lines to the
deterministic rules extractor — no LLM involved.

Engine selection is a pure infrastructure concern: both implementations
satisfy the same tiny protocol, so swapping them (or adding
PP-Structure layout parsing later) never touches domain or application
layers.
"""

import io
import logging
import re
from typing import Protocol

from app.domain.exceptions import OCRExtractionError

logger = logging.getLogger(__name__)


class OcrEngine(Protocol):
    """Minimal contract: image bytes -> text lines in reading order."""

    def lines(self, image_bytes: bytes) -> list[str]: ...


class TesseractLinesEngine:
    """Tesseract-based engine; zero extra dependencies in the image."""

    def __init__(self, language: str = "eng+spa") -> None:
        self._language = language

    def lines(self, image_bytes: bytes) -> list[str]:
        import pytesseract
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(image_bytes))
            text = pytesseract.image_to_string(image, lang=self._language)
        except Exception as exc:
            raise OCRExtractionError(f"Tesseract failed: {exc}") from exc
        return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _result_field(page, key):
    """PaddleOCR result objects expose fields as attributes; .get() lies."""
    value = getattr(page, key, None)
    if value is None and hasattr(page, "get"):
        value = page.get(key)
    return value


class PaddleOCREngine:
    """PP-OCR via PaddleOCR (det + rec). Models download on first use."""

    def __init__(self, language: str = "es") -> None:
        self._language = language
        self._ocr = None  # lazy: keeps paddle off the api/streamlit path

    def _engine(self):
        if self._ocr is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:  # pragma: no cover - image ships it
                raise OCRExtractionError(
                    "paddleocr no está instalado en esta imagen"
                ) from exc
            logger.info("Inicializando PaddleOCR (lang=%s); primer uso descarga modelos", self._language)
            # Version-tolerant kwargs: v3 accepts these flags; older
            # versions ignore unknown ones or take fewer options.
            try:
                self._ocr = PaddleOCR(
                    lang=self._language,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                )
            except TypeError:
                self._ocr = PaddleOCR(lang=self._language)
        return self._ocr

    def lines(self, image_bytes: bytes) -> list[str]:
        import numpy as np
        from PIL import Image

        image = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        engine = self._engine()
        try:
            result = engine.predict(image)
        except AttributeError:  # paddleocr < 3.0
            result = engine.ocr(image, cls=False)
        return self._collect_lines(result)

    @staticmethod
    def _collect_lines(result) -> list[str]:
        """Flatten PaddleOCR output across known result shapes."""
        texts: list[str] = []
        for page in result or []:
            # v3: dict-like object with rec_texts; v2: [[box, (text, conf)]]
            rec_texts = _result_field(page, "rec_texts")
            if isinstance(rec_texts, (list, tuple)):
                texts.extend(str(t) for t in rec_texts)
                continue
            if isinstance(rec_texts, str) and rec_texts.strip():
                texts.append(rec_texts)
                continue
            for line in page or []:
                try:
                    texts.append(str(line[1][0]))
                except (IndexError, TypeError):
                    continue
        return [t.strip() for t in texts if t and t.strip()]


class PaddleOCRVLEngine:
    """PaddleOCR-VL pipeline (VL 1.5/0.9B + PP-DocLayout + PP-OCR).

    Document-parsing pipeline: layout analysis (PP-Structure family),
    PP-OCRv5 detection/recognition and the PaddleOCR-VL vision-language
    recognizer. Returns plain text lines extracted from the pipeline's
    markdown output so the rules parser can work on them unchanged.
    """

    def __init__(self, language: str = "es") -> None:
        self._pipeline = None  # lazy

    def _engine(self):
        if self._pipeline is None:
            try:
                from paddleocr import PaddleOCRVL
            except ImportError as exc:  # pragma: no cover
                raise OCRExtractionError(
                    "paddleocr>=3.3 con soporte PaddleOCR-VL no está instalado"
                ) from exc
            logger.info("Inicializando pipeline PaddleOCR-VL; primer uso descarga modelos")
            try:
                self._pipeline = PaddleOCRVL()
            except TypeError:  # versiones sin flags avanzados
                self._pipeline = PaddleOCRVL(use_textline_orientation=False)
        return self._pipeline

    def lines(self, image_bytes: bytes) -> list[str]:
        import numpy as np
        from PIL import Image

        image = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        result = self._engine().predict(image)
        return self._collect_lines(result)

    @staticmethod
    def _collect_lines(result) -> list[str]:
        """Extract reading-order text from PaddleOCR-VL results."""
        texts: list[str] = []
        for page in result or []:
            md = _result_field(page, "markdown")
            if isinstance(md, dict):
                # paddleocr>=3.3: {"markdown_texts": "...", "markdown_images": ...}
                md = md.get("markdown_texts") or md.get("text")
            if md:
                # Tables come as raw HTML: split tags into lines so the
                # rules regexes can see individual cells ($136.135, ...).
                cleaned = re.sub(r"<[^>]+>", "\n", str(md))
                texts.extend(cleaned.splitlines())
                continue
            texts.extend(PaddleOCREngine._collect_lines([page]))
        return [t.strip() for t in texts if t and t.strip() and t.strip() != "---"]


def build_ocr_engine(engine_name: str, language: str) -> OcrEngine:
    engines = {
        "vl": PaddleOCRVLEngine,
        "paddle": PaddleOCREngine,
        "tesseract": TesseractLinesEngine,
    }
    engine_cls = engines.get(engine_name)
    if engine_cls is None:
        raise ValueError(
            f"local_ocr_engine must be one of {sorted(engines)}, got {engine_name!r}"
        )
    return engine_cls(language=language)


__all__ = [
    "OcrEngine",
    "PaddleOCREngine",
    "PaddleOCRVLEngine",
    "TesseractLinesEngine",
    "build_ocr_engine",
]
