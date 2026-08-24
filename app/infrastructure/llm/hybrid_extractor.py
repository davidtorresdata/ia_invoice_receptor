"""Hybrid extraction: deterministic rules first, vision model on miss.

Flow (matches the architecture diagram):

    OCR text ──> RulesInvoiceExtractor ── ok ──> ExtractedInvoiceData
                     │
                      ├── PartialExtractionError (algunos campos hallados)
                      │        ▼
                      │   Fallback segun LLM_EXECUTION:
                      │     api   -> Gemini/OpenAI vision call
                      │     local -> PaddleOCR-VL (PP-Structure + PP-OCR)
                     │        ▼
                     │   MERGE simétrico por campo:
                     │     - todo campo hallado por reglas GANA (incluido
                     │       el trío subtotal/tax/total, que viaja como
                     │       bloque atómico para preservar la aritmética)
                     │     - campos faltantes -> modelo de visión
                     │
                     └── LLMExtractionError (nada encontrado)
                              ▼
                     visión 100%, sin fusión

Business validation downstream decides persistence vs. review, exactly as
with any other provider.

The fallback is optional at construction time: when no vision credentials
are configured the hybrid behaves like plain rules and re-raises the
original pattern-miss error instead of failing on a missing API key.
"""

import logging

from app.domain.exceptions import LLMExtractionError, PartialExtractionError
from app.domain.services.invoice_extractor import InvoiceExtractor
from app.domain.value_objects.extracted_invoice import ExtractedInvoiceData

logger = logging.getLogger(__name__)


class HybridInvoiceExtractor(InvoiceExtractor):
    def __init__(
        self,
        primary: InvoiceExtractor,
        fallback: InvoiceExtractor | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    def extract(
        self,
        document_text: str,
        images=None,
    ) -> ExtractedInvoiceData:
        try:
            data = self._primary.extract(document_text)
            logger.info(
                "Extraccion hibrida: resuelto con reglas (sin llamada al modelo de vision)"
            )
            return data
        except PartialExtractionError as exc:
            if self._fallback is None:
                logger.warning(
                    "Reglas hallaron solo campos parciales y no hay fallback "
                    "de vision configurado (define LLM_API_KEY o LLM_BASE_URL)"
                )
                raise
            logger.warning(
                "Extraccion hibrida: reglas parciales (faltantes: %s) -> escalando a %s",
                exc.missing_fields,
                type(self._fallback).__name__,
            )
            vision = self._fallback.extract(document_text, images=images)
            from_rules, merged = self._merge(exc.partial_data, vision)
            logger.info(
                "Fusion hibrida: %s desde reglas; campos faltantes desde vision",
                ", ".join(sorted(from_rules)) or "nada",
            )
            return merged
        except LLMExtractionError as exc:
            if self._fallback is None:
                logger.warning(
                    "Reglas no hallaron patron y no hay fallback de vision "
                    "configurado (define LLM_API_KEY o LLM_BASE_URL)"
                )
                raise
            logger.warning(
                "Extraccion hibrida: reglas sin patron (%s) -> escalando a %s",
                str(exc)[:160],
                type(self._fallback).__name__,
            )
            return self._fallback.extract(document_text, images=images)

    # ------------------------------------------------------------------ merge
    @staticmethod
    def _merge(
        partial: dict, vision: ExtractedInvoiceData
    ) -> tuple[list[str], ExtractedInvoiceData]:
        """Symmetric merge: any field found by the rules wins; vision fills gaps."""
        base = vision.model_dump()
        from_rules: list[str] = []
        for field, value in partial.items():
            if value is None:
                continue
            base[field] = value
            from_rules.append(field)
        return from_rules, ExtractedInvoiceData.model_validate(base)


__all__ = ["HybridInvoiceExtractor"]
