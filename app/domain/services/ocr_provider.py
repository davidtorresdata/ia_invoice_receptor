"""OCR port — text extraction abstraction (provider-agnostic)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.value_objects.enums import DocumentType


@dataclass(frozen=True, slots=True)
class OCRResult:
    """Outcome of a text-extraction pass over one document."""

    text: str
    page_count: int
    method: str  # e.g. "embedded-text", "tesseract", "embedded+tesseract"

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class OCRProvider(ABC):
    """
    Driven port for document text extraction.

    Implementations decide *when* real OCR runs (e.g., only for scanned PDFs
    or images) and *which engine* performs it (Tesseract today; AWS Textract,
    Azure Document Intelligence, Google Document AI tomorrow). The rest of
    the system only depends on this interface.
    """

    @abstractmethod
    def extract_text(self, content: bytes, document_type: DocumentType) -> OCRResult:
        """
        Extract textual content from a document payload.

        Raises:
            OCRExtractionError: when extraction fails irrecoverably.
        """
