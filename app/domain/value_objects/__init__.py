"""Immutable domain value objects."""

from app.domain.value_objects.enums import (
    DocumentStatus,
    DocumentType,
    JobStatus,
)
from app.domain.value_objects.extracted_invoice import (
    ExtractedInvoiceData,
    ExtractedItem,
    ExtractedSupplier,
)
from app.domain.value_objects.file_type import FileType
from app.domain.value_objects.money import Money
from app.domain.value_objects.validation import Severity, ValidationIssue, ValidationReport

__all__ = [
    "DocumentStatus",
    "DocumentType",
    "ExtractedInvoiceData",
    "ExtractedItem",
    "ExtractedSupplier",
    "FileType",
    "JobStatus",
    "Money",
    "Severity",
    "ValidationIssue",
    "ValidationReport",
]
