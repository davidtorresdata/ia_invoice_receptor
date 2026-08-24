"""Domain enumerations (state machines and classifications)."""

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle of an async processing job."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED}


class DocumentStatus(StrEnum):
    """Lifecycle of an uploaded document."""

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class DocumentType(StrEnum):
    """Coarse classification driving the text-extraction strategy."""

    PDF = "PDF"
    IMAGE = "IMAGE"


