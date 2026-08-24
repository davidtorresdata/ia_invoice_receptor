"""FastAPI dependency providers — thin wrappers over the composition root.

Tests override these functions via `app.dependency_overrides`, which is the
single seam needed to run the API against fakes (see tests/e2e).
"""

from app.application.use_cases.dashboard_stats import DashboardStatsUseCase
from app.application.use_cases.get_invoice import GetInvoiceUseCase
from app.application.use_cases.get_job_status import GetJobStatusUseCase
from app.application.use_cases.list_invoices import ListInvoicesUseCase
from app.application.use_cases.upload_invoice import UploadInvoiceUseCase
from app.infrastructure import container


def get_upload_invoice_use_case() -> UploadInvoiceUseCase:
    return container.build_upload_invoice_use_case()


def get_get_invoice_use_case() -> GetInvoiceUseCase:
    return container.build_get_invoice_use_case()


def get_list_invoices_use_case() -> ListInvoicesUseCase:
    return container.build_list_invoices_use_case()


def get_job_status_use_case() -> GetJobStatusUseCase:
    return container.build_job_status_use_case()


def get_dashboard_stats_use_case() -> DashboardStatsUseCase:
    return container.build_dashboard_stats_use_case()
