"""Local execution mode: PaddleOCR-VL over rendered pages + rules.

Replaces the previous LLM-based local fallback (Qwen3-VL via Ollama):
no model server, no GPU, no network. The rendered page bitmaps go
through the configured OCR engine (PaddleOCR-VL / PP-OCR by default)
and the recognized lines are parsed by the SAME deterministic
RulesInvoiceExtractor used as hybrid primary — one parser, two text
sources (embedded layer vs. OCR layer).

Fusion strategy (dual source): rules run on BOTH the document's embedded
text and the OCR text; their partial payloads are unioned (the monetary
trio stays atomic, owned by whichever source found the total). When the
union covers every required field a full ExtractedInvoiceData is
returned — indistinguishable, for the hybrid orchestrator and downstream
validation, from a vision-model result. Otherwise a PartialExtractionError
carries the best-effort union with the same contract as always.
"""

import logging

from pydantic import ValidationError

from app.domain.exceptions import LLMExtractionError, PartialExtractionError
from app.domain.services.invoice_extractor import InvoiceExtractor
from app.domain.value_objects.extracted_invoice import ExtractedInvoiceData
from app.infrastructure.llm.rules_extractor import (
    RulesInvoiceExtractor,
    ensure_items_payload,
)
from app.infrastructure.ocr.engines import OcrEngine

logger = logging.getLogger(__name__)

_REQUIRED = ("number", "supplier", "issue_date", "total")
_AMOUNT_KEYS = ("subtotal", "tax", "total")


class LocalOCRInvoiceExtractor(InvoiceExtractor):
    """PaddleOCR-VL/PP-OCR + reglas: extraccion 100% local, sin LLM."""

    def __init__(self, engine: OcrEngine) -> None:
        self._engine = engine
        self._rules = RulesInvoiceExtractor()

    def extract(
        self,
        document_text: str,
        images=None,
    ) -> ExtractedInvoiceData:
        if not images:
            raise LLMExtractionError(
                "Modo local: no hay paginas renderizadas para OCR "
                "(el documento no pudo rasterizarse)"
            )
        ocr_text = self._ocr_text(images)
        return self._fuse(document_text or "", ocr_text)

    # ------------------------------------------------------------------ internals
    def _ocr_text(self, images) -> str:
        lines: list[str] = []
        for page_number, image_bytes in enumerate(images, start=1):
            page_lines = self._engine.lines(image_bytes)
            logger.info(
                "PP-OCR pagina %s/%s: %s lineas reconocidas",
                page_number, len(images), len(page_lines),
            )
            lines.extend(page_lines)
        return "\n".join(lines)

    def _fuse(self, embedded_text: str, ocr_text: str) -> ExtractedInvoiceData:
        partials: list[dict] = []
        for label, source in (("texto embebido", embedded_text), ("OCR", ocr_text)):
            if not source.strip():
                continue
            try:
                data = self._rules.extract(source)
                logger.info("Modo local: extraccion completa desde %s", label)
                return data
            except PartialExtractionError as exc:
                partials.append(exc.partial_data)
            except LLMExtractionError:
                continue  # nothing found in this source

        merged = self._merge_partials(partials)
        if not merged:
            raise LLMExtractionError(
                "Ni la capa de texto ni el OCR local hallaron campos de factura"
            )

        payload = dict(merged)
        ensure_items_payload(payload)
        try:
            data = ExtractedInvoiceData.model_validate(payload)
        except ValidationError:
            missing = [f for f in _REQUIRED if not merged.get(f)]
            raise PartialExtractionError(
                "Fusion local incompleta; campos recuperables entregados.",
                partial_data=merged,
                missing_fields=missing,
            ) from None
        logger.info(
            "Modo local: fusion texto+OCR completa (%s campos)",
            len(merged),
        )
        return data

    @staticmethod
    def _merge_partials(partials: list[dict]) -> dict:
        merged: dict = {}
        for partial in partials:
            for key, value in partial.items():
                if value in (None, [], {}):
                    continue
                merged.setdefault(key, value)
        # The monetary trio travels atomically, owned by whichever source
        # actually found the total (mirrors the hybrid merge contract).
        owner = next((p for p in partials if p.get("total")), None)
        if owner:
            for key in _AMOUNT_KEYS:
                if key in owner:
                    merged[key] = owner[key]
        return merged


__all__ = ["LocalOCRInvoiceExtractor"]
