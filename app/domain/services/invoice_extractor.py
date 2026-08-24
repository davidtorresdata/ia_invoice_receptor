"""LLM port — structured invoice extraction contract."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.domain.value_objects.extracted_invoice import ExtractedInvoiceData


class InvoiceExtractor(ABC):
    """
    Driven port turning raw document text into *validated* structured data.

    Contract:
      - Implementations MUST return an `ExtractedInvoiceData` instance
        (Pydantic-validated) or raise `LLMExtractionError`.
      - Arbitrary/unvalidated LLM output must never escape this boundary.
      - Timeout, retries and provider choice are adapter concerns.
      - `images` carries page renderings (PNG/JPG bytes) for vision-capable
        adapters; text-only adapters must ignore it.
    """

    @abstractmethod
    def extract(
        self,
        document_text: str,
        images: Sequence[bytes] | None = None,
    ) -> ExtractedInvoiceData:
        """
        Convert document text into structured, validated invoice data.

        Raises:
            LLMExtractionError: on transport failure, timeout, invalid JSON,
                schema mismatch after internal attempts, etc.
        """
