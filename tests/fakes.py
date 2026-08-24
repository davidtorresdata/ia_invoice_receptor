"""Shared in-memory fakes implementing the domain/application ports.

These let the whole system be exercised without PostgreSQL, Redis or any
external service — direct proof of the hexagonal boundaries.
"""

from collections.abc import Callable
from uuid import UUID

from app.application.services.task_dispatcher import TaskDispatcher
from app.domain.exceptions import StorageError
from app.domain.services.document_storage import DocumentStorage
from app.domain.services.invoice_extractor import InvoiceExtractor
from app.domain.services.ocr_provider import OCRProvider, OCRResult
from app.domain.value_objects.extracted_invoice import ExtractedInvoiceData
from tests.fakes_uow import FakeUnitOfWork


class FakeStorage(DocumentStorage):
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.fail_on_get = False

    def save(self, document_id: UUID, filename: str, content: bytes) -> str:
        key = f"fake/{document_id}/{filename}"
        self.files[key] = content
        return key

    def get(self, storage_key: str) -> bytes:
        self._ensure_not_traversal(storage_key)
        if self.fail_on_get or storage_key not in self.files:
            raise StorageError(f"missing: {storage_key}", retryable=False)
        return self.files[storage_key]

    def delete(self, storage_key: str) -> None:
        self.files.pop(storage_key, None)


class FakeOCR(OCRProvider):
    def __init__(self, text: str = "INVOICE #FAKE-1") -> None:
        self.text = text
        self.calls = 0
        self.fail_with: Exception | None = None

    def extract_text(self, content: bytes, document_type) -> OCRResult:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return OCRResult(text=self.text, page_count=1, method="fake")


class FakeLLM(InvoiceExtractor):
    """Returns a canned payload or raises the configured exception."""

    def __init__(self, data: ExtractedInvoiceData | None = None,
                 exc: Exception | None = None) -> None:
        self.data = data
        self.exc = exc
        self.calls = 0
        self.last_images: list[bytes] | None = None

    def extract(self, document_text: str, images=None) -> ExtractedInvoiceData:
        self.calls += 1
        self.last_images = list(images) if images else []
        if self.exc is not None:
            raise self.exc
        assert self.data is not None
        return self.data


class RecordingDispatcher(TaskDispatcher):
    """Records enqueues; optionally executes the pipeline inline (e2e)."""

    def __init__(self, inline_handler: Callable[[UUID], None] | None = None) -> None:
        self.dispatched: list[UUID] = []
        self._handler = inline_handler

    def dispatch_invoice_processing(self, job_id: UUID) -> None:
        self.dispatched.append(job_id)
        if self._handler is not None:
            self._handler(job_id)


def make_valid_extraction(**overrides) -> ExtractedInvoiceData:
    """A mathematically consistent extraction payload for tests."""
    base: dict = {
        "number": "INV-2026-001",
        "date": "2026-01-15",
        "due_date": "2026-02-14",
        "currency": "EUR",
        "subtotal": "1000.00",
        "tax": "210.00",
        "total": "1210.00",
        "supplier": {
            "name": "Acme Supplies Ltd.",
            "tax_id": "B87654321",
            "address": "1 Acme Road",
            "email": "billing@acme.example",
        },
        "items": [
            {"description": "Consulting", "quantity": 4, "unit_price": "150.00",
             "tax": "126.00", "total": "600.00"},
            {"description": "Support pack", "quantity": 2, "unit_price": "200.00",
             "tax": "84.00", "total": "400.00"},
        ],
    }
    base.update(overrides)
    return ExtractedInvoiceData.model_validate(base)


__all__ = [
    "FakeLLM",
    "FakeOCR",
    "FakeStorage",
    "FakeUnitOfWork",
    "RecordingDispatcher",
    "make_valid_extraction",
]
