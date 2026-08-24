"""Extraction factory (provider + execution-mode swap point).

LLM_EXECUTION switch (hexagonal composition root):

    api   -> HybridInvoiceExtractor with a remote vision fallback
             (OpenAI-compatible: Gemini, OpenAI, DashScope...)
    local -> HybridInvoiceExtractor with LocalOCRInvoiceExtractor:
             PP-OCR (PaddleOCR) over rendered pages + the same rules
             parser. Fully offline, no LLM, no model server.

The port (InvoiceExtractor), the hybrid flow and every downstream
consumer stay identical in both positions.
"""

import logging

from app.config.settings import Settings
from app.domain.exceptions import ConfigurationError
from app.domain.services.invoice_extractor import InvoiceExtractor
from app.infrastructure.llm.hybrid_extractor import HybridInvoiceExtractor
from app.infrastructure.llm.local_ocr_extractor import LocalOCRInvoiceExtractor
from app.infrastructure.llm.mock_extractor import MockInvoiceExtractor
from app.infrastructure.llm.openai_extractor import OpenAICompatibleInvoiceExtractor
from app.infrastructure.llm.rules_extractor import RulesInvoiceExtractor
from app.infrastructure.ocr.engines import build_ocr_engine

logger = logging.getLogger(__name__)


def _build_api_vision(settings: Settings) -> OpenAICompatibleInvoiceExtractor:
    """Vision adapter against the remote OpenAI-compatible endpoint."""
    api_key = settings.llm_api_key.get_secret_value()
    base_url = settings.llm_base_url or ""
    if not (api_key or base_url):
        raise ConfigurationError(
            "El modo api requiere LLM_API_KEY o LLM_BASE_URL "
            "(ej. Gemini/OpenAI/DashScope)"
        )
    return OpenAICompatibleInvoiceExtractor(
        api_key=api_key,
        model=settings.llm_model,
        base_url=base_url or None,
        timeout_seconds=float(settings.llm_timeout_seconds),
        temperature=settings.llm_temperature,
        max_attempts=settings.llm_max_attempts,
    )


def _build_local_fallback(settings: Settings) -> LocalOCRInvoiceExtractor:
    """PP-OCR/Tesseract over page images + rules parser (sin LLM)."""
    engine = build_ocr_engine(settings.local_ocr_engine, settings.local_ocr_lang)
    logger.info(
        "Fallback local: OCR %s (lang=%s) + RulesInvoiceExtractor",
        settings.local_ocr_engine, settings.local_ocr_lang,
    )
    return LocalOCRInvoiceExtractor(engine=engine)


def build_invoice_extractor(settings: Settings) -> InvoiceExtractor:
    """
    Composition root for the extraction port.

    Providers:
      hybrid   rules first; on pattern-miss escalate per LLM_EXECUTION
               (default)
      rules    offline regex parsing only
      openai   always the remote OpenAI-compatible vision model
      mock     synthetic output for demos/tests

    Adding a provider = one new adapter + one entry here.
    """
    normalized = settings.llm_provider.strip().lower()
    local_mode = settings.llm_execution == "local"

    if normalized == "mock":
        logger.warning("Using MockInvoiceExtractor — output is synthetic!")
        return MockInvoiceExtractor()

    if normalized in ("rules", "local"):
        logger.info("Using RulesInvoiceExtractor — offline heuristic parsing")
        return RulesInvoiceExtractor()

    if normalized == "openai":
        logger.info(
            "Using vision extractor (api/%s)", settings.llm_model,
        )
        return _build_api_vision(settings)

    if normalized == "hybrid":
        fallback = _build_local_fallback(settings) if local_mode else None
        if fallback is None:
            # Remote mode: only wire the vision fallback when credentials exist;
            # otherwise hybrid degrades to plain rules (same as before).
            try:
                fallback = _build_api_vision(settings)
            except ConfigurationError:
                fallback = None
        mode = (
            f"local/{settings.local_ocr_engine}"
            if local_mode else f"api/{settings.llm_model}"
        )
        logger.info("Using HybridInvoiceExtractor (rules + %s)", mode)
        return HybridInvoiceExtractor(
            primary=RulesInvoiceExtractor(),
            fallback=fallback,
        )

    raise ConfigurationError(f"Unknown LLM_PROVIDER '{settings.llm_provider}'")


__all__ = ["build_invoice_extractor"]
