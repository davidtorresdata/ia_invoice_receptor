"""Use case: dashboard aggregates for the Streamlit home page."""

from collections.abc import Callable

from app.application.dto.dashboard_stats import DashboardStats
from app.application.services.unit_of_work import UnitOfWork
from app.domain.value_objects.enums import JobStatus


class DashboardStatsUseCase:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self) -> DashboardStats:
        with self._uow_factory() as uow:
            job_counts = uow.jobs.count_by_status()
            invoice_stats = uow.invoices.stats()

        return DashboardStats(
            pending_jobs=job_counts.get(JobStatus.PENDING.value, 0),
            processing_jobs=job_counts.get(JobStatus.PROCESSING.value, 0),
            completed_jobs=job_counts.get(JobStatus.COMPLETED.value, 0),
            failed_jobs=job_counts.get(JobStatus.FAILED.value, 0),
            total_invoices=invoice_stats.total_invoices,
            total_invoiced=invoice_stats.total_invoiced,
        )
