"""OCR provider factory (extension point for cloud OCR services)."""

from app.config.settings import Settings
from app.domain.exceptions import ConfigurationError
from app.domain.services.ocr_provider import OCRProvider
from app.infrastructure.ocr.local_ocr import LocalOCRProvider


def build_ocr_provider(settings: Settings) -> OCRProvider:
    """
    Composition root for text extraction.

    Future providers plug in here, e.g.:
        if provider == "aws-textract": return TextractOCRProvider(...)
    """
    normalized = settings.ocr_provider.strip().lower()
    if normalized == "local":
        return LocalOCRProvider(
            language=settings.ocr_language,
            min_text_chars_per_page=settings.ocr_min_text_chars_per_page,
        )
    raise ConfigurationError(f"Unknown OCR_PROVIDER '{settings.ocr_provider}'")


__all__ = ["build_ocr_provider"]
