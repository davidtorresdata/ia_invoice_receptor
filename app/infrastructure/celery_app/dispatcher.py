"""Task dispatcher adapter: implements the application port with Celery."""

import logging

from celery import Celery
from kombu.exceptions import OperationalError
from kombu.exceptions import TimeoutError as KombuTimeoutError

from app.application.services.task_dispatcher import TaskDispatcher
from app.domain.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)


class CeleryTaskDispatcher(TaskDispatcher):
    def __init__(self, celery_application: Celery) -> None:
        self._app = celery_application

    def dispatch_invoice_processing(self, job_id) -> None:
        from uuid import UUID

        job_uuid = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
        try:
            async_result = self._app.send_task(
                "process_invoice",
                args=[str(job_uuid)],
                queue="invoices",
            )
        except (OperationalError, KombuTimeoutError, ConnectionError) as exc:
            raise ExternalServiceError(f"Broker unavailable: {exc}") from exc
        logger.info(
            "Job dispatched to queue",
            extra={"job_id": str(job_uuid), "task_id": getattr(async_result, "id", None)},
        )


__all__ = ["CeleryTaskDispatcher"]
