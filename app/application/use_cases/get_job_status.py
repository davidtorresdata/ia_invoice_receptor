"""Use case: fetch the status of an async processing job."""

from collections.abc import Callable
from uuid import UUID

from app.application.services.unit_of_work import UnitOfWork
from app.domain.entities.job import ProcessingJob
from app.domain.exceptions import JobNotFoundError


class GetJobStatusUseCase:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self, job_id: UUID) -> ProcessingJob:
        with self._uow_factory() as uow:
            job = uow.jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        return job
