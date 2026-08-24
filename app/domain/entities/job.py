"""ProcessingJob entity: tracks async pipeline execution for a document."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.exceptions import EntityValidationError
from app.domain.value_objects.enums import JobStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class ProcessingJob:
    """
    Unit of asynchronous work. Created PENDING on upload; the Celery task
    drives its state machine: PROCESSING -> COMPLETED | FAILED (with retries
    staying in PROCESSING while attempts increment).
    """

    document_id: UUID
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    invoice_id: UUID | None = None
    celery_task_id: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.attempts < 0:
            raise EntityValidationError("Job attempts cannot be negative")

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def can_be_processed(self) -> bool:
        """PENDING (first run), FAILED (manual requeue) or PROCESSING (retry/redelivery)."""
        return self.status != JobStatus.COMPLETED

    def attach_task(self, celery_task_id: str) -> None:
        self.celery_task_id = celery_task_id
        self._touch()

    def start(self) -> None:
        if self.status == JobStatus.COMPLETED:
            raise EntityValidationError("Cannot restart a completed job")
        self.status = JobStatus.PROCESSING
        self.attempts += 1
        if self.started_at is None:
            self.started_at = _utcnow()
        self._touch()

    def complete(self, invoice_id: UUID | None = None) -> None:
        if self.status == JobStatus.FAILED:
            raise EntityValidationError("Cannot complete a failed job")
        self.status = JobStatus.COMPLETED
        if invoice_id is not None:
            self.invoice_id = invoice_id
        self.finished_at = _utcnow()
        self._touch()

    def fail(self, message: str) -> None:
        self.status = JobStatus.FAILED
        self.error_message = message[:2000]  # guard against unbounded payloads
        self.finished_at = _utcnow()
        self._touch()

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    def _touch(self) -> None:
        self.updated_at = _utcnow()
