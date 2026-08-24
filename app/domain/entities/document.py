"""Document entity: an uploaded invoice file and its lifecycle."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.exceptions import EntityValidationError
from app.domain.value_objects.enums import DocumentStatus, DocumentType


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Document:
    """An uploaded source document stored in blob storage + PostgreSQL metadata."""

    filename: str
    content_type: str
    size_bytes: int
    storage_path: str
    document_type: DocumentType
    id: UUID = field(default_factory=uuid4)
    status: DocumentStatus = DocumentStatus.RECEIVED
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.size_bytes < 0:
            raise EntityValidationError("Document size cannot be negative")
        if not self.filename:
            raise EntityValidationError("Document filename is required")

    @property
    def is_processed(self) -> bool:
        return self.status == DocumentStatus.PROCESSED

    def mark_processing(self) -> None:
        if self.status == DocumentStatus.PROCESSING:
            # Idempotent: a Celery retry re-enters the pipeline with the
            # document already in PROCESSING (resume, not a new transition).
            self._touch()
            return
        self._transition(DocumentStatus.RECEIVED, DocumentStatus.PROCESSING)

    def mark_processed(self) -> None:
        self._transition(DocumentStatus.PROCESSING, DocumentStatus.PROCESSED)

    def mark_failed(self) -> None:
        if self.status == DocumentStatus.PROCESSED:
            raise EntityValidationError("Cannot fail an already processed document")
        self.status = DocumentStatus.FAILED
        self._touch()

    def _transition(self, expected: DocumentStatus, target: DocumentStatus) -> None:
        if self.status != expected:
            raise EntityValidationError(
                f"Invalid document transition {self.status} -> {target} "
                f"(expected current state {expected})"
            )
        self.status = target
        self._touch()

    def _touch(self) -> None:
        self.updated_at = _utcnow()
