"""Celery task executing the invoice pipeline with controlled retries."""

import logging
import uuid

from celery.exceptions import SoftTimeLimitExceeded

from app.config.settings import get_settings
from app.domain.exceptions import AppError
from app.domain.value_objects.enums import JobStatus
from app.infrastructure.celery_app.app import celery_app
from app.infrastructure.container import build_process_invoice_use_case, build_uow

logger = logging.getLogger(__name__)

settings = get_settings()


@celery_app.task(
    bind=True,
    name="process_invoice",
    max_retries=settings.celery_max_retries,
)
def process_invoice_task(self, job_id: str) -> dict[str, str]:
    """
    Entry point executed by workers.

    Retry policy:
      * transient errors  -> exponential backoff retry while attempts remain;
      * exhausted retries -> job closed as FAILED here;
      * permanent errors  -> the use case already persisted FAILED before
        re-raising; the task simply reports the outcome.
    """
    ctx = {"job_id": job_id, "task_id": self.request.id}
    logger.info("Task received", extra={**ctx, "retries": self.request.retries})
    _attach_celery_task_id(uuid.UUID(job_id), self.request.id)

    use_case = build_process_invoice_use_case()
    try:
        use_case.execute(uuid.UUID(job_id))
    except SoftTimeLimitExceeded:
        _mark_job_failed(uuid.UUID(job_id), f"Processing timed out after "
                                            f"{settings.celery_task_timeout_seconds}s")
        return {"status": JobStatus.FAILED.value, "reason": "timeout"}
    except AppError as exc:
        return _handle_failure(self, exc, ctx)

    logger.info("Task finished successfully", extra=ctx)
    return {"status": JobStatus.COMPLETED.value}


# ---------------------------------------------------------------------------
def _handle_failure(task: object, exc: AppError, ctx: dict[str, str]) -> dict[str, str]:
    max_retries = int(settings.celery_max_retries)
    current = int(task.request.retries)  # type: ignore[attr-defined]

    if exc.retryable and current < max_retries:
        countdown = settings.celery_retry_backoff_seconds * (2 ** current)
        logger.warning(
            "Transient failure (%s); retry %s/%s in %ss",
            exc.code, current + 1, max_retries, countdown,
            extra={**ctx, "error_code": exc.code},
        )
        raise task.retry(countdown=countdown, exc=exc)  # type: ignore[attr-defined]

    if not exc.retryable:
        # FAILED already persisted by the use case; report without re-raising
        # to keep broker logs clean (DB remains the source of truth).
        logger.error("Permanent failure (%s): %s", exc.code, exc, extra=ctx)
        return {"status": JobStatus.FAILED.value, "reason": exc.code}

    logger.error("Retries exhausted (%s): %s", exc.code, exc, extra=ctx)
    _mark_job_failed(uuid.UUID(ctx["job_id"]), str(exc))
    return {"status": JobStatus.FAILED.value, "reason": "retries_exhausted"}


def _attach_celery_task_id(job_id: uuid.UUID, celery_task_id: str) -> None:
    try:
        with build_uow() as uow:
            job = uow.jobs.get(job_id)
            if job is not None and not job.is_terminal:
                job.attach_task(celery_task_id)
                uow.jobs.update(job)
                uow.commit()
    except Exception:  # pragma: no cover - observability nicety only
        logger.warning("Could not store celery task id", exc_info=True)


def _mark_job_failed(job_id: uuid.UUID, message: str) -> None:
    try:
        with build_uow() as uow:
            job = uow.jobs.get(job_id)
            document = uow.documents.get(job.document_id) if job else None
            if job is not None and not job.is_terminal:
                job.fail(message)
                uow.jobs.update(job)
            if document is not None and not document.is_processed:
                document.mark_failed()
                uow.documents.update(document)
            uow.commit()
    except Exception:  # pragma: no cover
        logger.exception("Could not persist FAILED state for job %s", job_id)
