"""Domain exception hierarchy.

Retry semantics contract (consumed by the Celery task layer):
    - `retryable=True`  -> transient failure: the task layer may retry.
    - `retryable=False` -> permanent failure: fail fast, mark job FAILED.

Required domain-facing exceptions are all defined here:
    DocumentProcessingError, OCRExtractionError, LLMExtractionError,
    BusinessValidationError (the business-rules "ValidationError"),
    PersistenceError, ExternalServiceError.
"""


class AppError(Exception):
    """Base class for every application-specific error."""

    default_retryable: bool = False
    error_code: str = "internal_error"

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.retryable = self.default_retryable if retryable is None else retryable

    @property
    def code(self) -> str:
        return self.error_code


# ---------------------------------------------------------------------------
# Domain errors (broken invariants / business rules)
# ---------------------------------------------------------------------------
class DomainError(AppError):
    error_code = "domain_error"


class EntityValidationError(DomainError):
    """An entity invariant was violated at construction/mutation time."""

    error_code = "entity_validation_error"


class BusinessValidationError(DomainError):
    """Invoice data violates business rules (math, required fields, dates)."""

    error_code = "business_validation_failed"

    def __init__(self, message: str, issues: list | None = None) -> None:
        super().__init__(message)
        self.issues = issues or []


# ---------------------------------------------------------------------------
# Application-level errors
# ---------------------------------------------------------------------------
class ApplicationError(AppError):
    error_code = "application_error"


class NotFoundError(ApplicationError):
    error_code = "not_found"


class DocumentNotFoundError(NotFoundError):
    error_code = "document_not_found"


class JobNotFoundError(NotFoundError):
    error_code = "job_not_found"


class InvoiceNotFoundError(NotFoundError):
    error_code = "invoice_not_found"


class InvalidFileError(ApplicationError):
    """Uploaded file rejected by extension / MIME / signature validation."""

    error_code = "invalid_file"


class FileTooLargeError(InvalidFileError):
    error_code = "file_too_large"


class EmptyFileError(InvalidFileError):
    error_code = "empty_file"


# ---------------------------------------------------------------------------
# Processing pipeline
# ---------------------------------------------------------------------------
class DocumentProcessingError(ApplicationError):
    """Generic document pipeline failure (non-retryable by default)."""

    error_code = "document_processing_failed"


class TransientPipelineError(DocumentProcessingError):
    """Recoverable pipeline hiccup worth a Celery retry."""

    default_retryable = True
    error_code = "transient_processing_error"


# ---------------------------------------------------------------------------
# External services (OCR, LLM, storage, ...) — transient by nature
# ---------------------------------------------------------------------------
class ExternalServiceError(AppError):
    default_retryable = True
    error_code = "external_service_error"


class OCRExtractionError(ExternalServiceError):
    error_code = "ocr_extraction_failed"


class LLMExtractionError(ExternalServiceError):
    error_code = "llm_extraction_failed"


class PartialExtractionError(LLMExtractionError):
    """Rules extraction found *some* fields but missed required ones.

    `partial_data` carries the successfully extracted subset (already
    JSON-compatible: dates as ISO strings, nested supplier/items as dicts)
    so a hybrid orchestrator can merge them with another provider's full
    result. The monetary trio (subtotal/tax/total) travels as one coherent
    block whenever the rules found it; otherwise the vision model supplies
    all three.
    """

    error_code = "llm_extraction_partial"

    def __init__(
        self,
        message: str,
        *,
        partial_data: dict,
        missing_fields: list[str],
    ) -> None:
        super().__init__(message, retryable=False)
        self.partial_data = partial_data
        self.missing_fields = missing_fields


class StorageError(ExternalServiceError):
    error_code = "storage_error"


# ---------------------------------------------------------------------------
# Infrastructure/persistence — usually transient (locks, deadlocks, pool)
# ---------------------------------------------------------------------------
class PersistenceError(AppError):
    default_retryable = True
    error_code = "persistence_error"


class ConfigurationError(AppError):
    """Misconfiguration detected at composition root / adapter build time."""

    error_code = "configuration_error"
