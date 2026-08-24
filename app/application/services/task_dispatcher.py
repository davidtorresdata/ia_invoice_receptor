"""Task dispatch port — decouples use cases from Celery/Redis."""

from abc import ABC, abstractmethod
from uuid import UUID


class TaskDispatcher(ABC):
    """Driven port to enqueue async work (Celery today, others tomorrow)."""

    @abstractmethod
    def dispatch_invoice_processing(self, job_id: UUID) -> None:
        """
        Enqueue processing of the given job.

        Raises:
            ExternalServiceError: when the broker is unreachable.
        """
