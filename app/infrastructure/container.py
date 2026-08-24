"""Composition root — the ONLY module that knows every concrete class.

Adapters are cached singletons (engines, clients); use cases are built fresh
per operation (stateless). Both FastAPI dependencies and Celery tasks import
from here, guaranteeing identical wiring everywhere.
"""

from functools import lru_cache

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.services.task_dispatcher import TaskDispatcher
from app.application.use_cases.dashboard_stats import DashboardStatsUseCase
from app.application.use_cases.get_invoice import GetInvoiceUseCase
from app.application.use_cases.get_job_status import GetJobStatusUseCase
from app.application.use_cases.list_invoices import ListInvoicesUseCase
from app.application.use_cases.process_invoice import ProcessInvoiceUseCase
from app.application.use_cases.upload_invoice import UploadInvoiceUseCase
from app.config.settings import Settings, get_settings
from app.domain.repositories.invoice_repository import InvoiceQuery
from app.domain.services.document_storage import DocumentStorage
from app.infrastructure.celery_app.app import celery_app
from app.infrastructure.celery_app.dispatcher import CeleryTaskDispatcher
from app.infrastructure.database.session import create_engine, create_session_factory
from app.infrastructure.llm import build_invoice_extractor
from app.infrastructure.llm.page_renderer import render_page_images
from app.infrastructure.ocr import build_ocr_provider
from app.infrastructure.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.storage import build_document_storage


# ---------------------------------------------------------------------------
# Settings / database
# ---------------------------------------------------------------------------
def get_app_settings() -> Settings:
    return get_settings()


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings())


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return create_session_factory(get_engine())


def build_uow() -> SqlAlchemyUnitOfWork:
    """Fresh unit of work per use-case execution (safe across threads)."""
    return SqlAlchemyUnitOfWork(get_session_factory())


# ---------------------------------------------------------------------------
# Adapters (singletons)
# ---------------------------------------------------------------------------
@lru_cache
def get_document_storage() -> DocumentStorage:
    return build_document_storage(get_settings())


@lru_cache
def get_task_dispatcher() -> TaskDispatcher:
    return CeleryTaskDispatcher(celery_app)


@lru_cache
def _get_ocr_provider():
    from app.domain.services.ocr_provider import OCRProvider  # local import: typing only

    provider: OCRProvider = build_ocr_provider(get_settings())
    return provider


@lru_cache
def _get_invoice_extractor():
    from app.domain.services.invoice_extractor import InvoiceExtractor  # typing only

    extractor: InvoiceExtractor = build_invoice_extractor(get_settings())
    return extractor


@lru_cache
def _get_invoice_validator():
    from app.domain.services.invoice_validator import InvoiceBusinessValidator

    return InvoiceBusinessValidator()


# ---------------------------------------------------------------------------
# Use cases (fresh per call)
# ---------------------------------------------------------------------------
def build_upload_invoice_use_case() -> UploadInvoiceUseCase:
    return UploadInvoiceUseCase(
        uow_factory=build_uow,
        storage=get_document_storage(),
        dispatcher=get_task_dispatcher(),
        max_file_size_bytes=get_settings().max_file_size_bytes,
    )


def build_process_invoice_use_case() -> ProcessInvoiceUseCase:
    settings = get_settings()
    return ProcessInvoiceUseCase(
        uow_factory=build_uow,
        storage=get_document_storage(),
        ocr_provider=_get_ocr_provider(),          # type: ignore[arg-type]
        extractor=_get_invoice_extractor(),        # type: ignore[arg-type]
        validator=_get_invoice_validator(),        # type: ignore[arg-type]
        page_renderer=lambda content, doc_type: render_page_images(
            content, doc_type, max_pages=settings.vision_max_pages
        ),
    )


def build_get_invoice_use_case() -> GetInvoiceUseCase:
    return GetInvoiceUseCase(uow_factory=build_uow)


def build_list_invoices_use_case() -> ListInvoicesUseCase:
    return ListInvoicesUseCase(uow_factory=build_uow)


def build_job_status_use_case() -> GetJobStatusUseCase:
    return GetJobStatusUseCase(uow_factory=build_uow)


def build_dashboard_stats_use_case() -> DashboardStatsUseCase:
    return DashboardStatsUseCase(uow_factory=build_uow)


def build_invoice_query(
    *,
    search: str | None = None,
    date_from=None,
    date_to=None,
    limit: int = 20,
    offset: int = 0,
) -> InvoiceQuery:
    return InvoiceQuery(
        search=search,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
