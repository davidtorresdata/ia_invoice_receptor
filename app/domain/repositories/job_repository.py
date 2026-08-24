"""Port for processing-job persistence."""

from abc import ABC, abstractmethod
from uuid import UUID

from app.domain.entities.job import ProcessingJob


class JobRepository(ABC):
    @abstractmethod
    def add(self, job: ProcessingJob) -> None:
        """Register a new processing job."""

    @abstractmethod
    def get(self, job_id: UUID) -> ProcessingJob | None:
        """Fetch by id or None."""

    @abstractmethod
    def update(self, job: ProcessingJob) -> None:
        """Persist state changes."""

    @abstractmethod
    def count_by_status(self) -> dict[str, int]:
        """Job counters grouped by status, e.g. {"PENDING": 3, ...}."""
