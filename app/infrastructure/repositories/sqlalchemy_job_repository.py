"""SQLAlchemy implementation of the JobRepository port."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.entities.job import ProcessingJob
from app.domain.exceptions import PersistenceError
from app.domain.repositories.job_repository import JobRepository
from app.domain.value_objects.enums import JobStatus
from app.infrastructure.database.models import ProcessingJobModel
from app.infrastructure.repositories.mappers import apply_job, job_to_domain


class SqlAlchemyJobRepository(JobRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, job: ProcessingJob) -> None:
        self._session.add(
            ProcessingJobModel(
                id=job.id,
                document_id=job.document_id,
                invoice_id=job.invoice_id,
                status=JobStatus(job.status),
                attempts=job.attempts,
                celery_task_id=job.celery_task_id,
                error_message=job.error_message,
                started_at=job.started_at,
                finished_at=job.finished_at,
            )
        )

    def get(self, job_id: uuid.UUID) -> ProcessingJob | None:
        model = self._session.get(ProcessingJobModel, job_id)
        return job_to_domain(model) if model else None

    def update(self, job: ProcessingJob) -> None:
        model = self._session.get(ProcessingJobModel, job.id)
        if model is None:
            raise PersistenceError(f"Job {job.id} not found for update")
        apply_job(job, model)

    def count_by_status(self) -> dict[str, int]:
        rows = self._session.execute(
            select(ProcessingJobModel.status, func.count()).group_by(ProcessingJobModel.status)
        ).all()
        counts: dict[str, int] = {}
        for status_value, total in rows:
            key = status_value.value if hasattr(status_value, "value") else str(status_value)
            counts[key] = int(total)
        return counts
