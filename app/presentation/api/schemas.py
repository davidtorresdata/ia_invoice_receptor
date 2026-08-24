"""Request/response schemas (Pydantic v2). Transport contracts only."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
class UploadResponse(BaseModel):
    document_id: UUID
    job_id: UUID
    filename: str
    status: str
    poll_url: str


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
class JobResponse(BaseModel):
    id: UUID
    document_id: UUID
    invoice_id: UUID | None
    status: str
    attempts: int
    error_message: str | None = None
    celery_task_id: str | None = None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------
class SupplierResponse(BaseModel):
    id: UUID
    name: str
    tax_id: str
    address: str | None
    phone: str | None
    email: str | None


class InvoiceItemResponse(BaseModel):
    id: UUID
    description: str
    quantity: Decimal
    unit_price: float
    tax: float
    total: float


class InvoiceResponse(BaseModel):
    id: UUID
    document_id: UUID
    number: str
    issue_date: date
    due_date: date | None
    currency: str
    subtotal: float
    tax: float
    total: float
    validation_report: dict | None
    raw_extraction: dict | None
    supplier: SupplierResponse
    items: list[InvoiceItemResponse]
    created_at: datetime


class InvoiceSummaryResponse(BaseModel):
    id: UUID
    document_id: UUID
    number: str
    issue_date: date
    due_date: date | None
    currency: str
    subtotal: float
    tax: float
    total: float
    supplier_name: str
    supplier_tax_id: str


class PaginatedInvoicesResponse(BaseModel):
    items: list[InvoiceSummaryResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Dashboard / health / errors
# ---------------------------------------------------------------------------
class DashboardStatsResponse(BaseModel):
    jobs: dict[str, int] = Field(
        description="Counters by job status: pending/processing/completed/failed"
    )
    invoices: dict[str, int]
    total_invoiced: float


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
