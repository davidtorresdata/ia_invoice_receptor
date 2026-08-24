"""Entity -> schema translators (keeps routers free of mapping noise)."""

from app.domain.entities.document import Document
from app.domain.entities.invoice import Invoice, Supplier
from app.domain.entities.job import ProcessingJob
from app.domain.repositories.invoice_repository import InvoiceSummary
from app.presentation.api.schemas import (
    InvoiceItemResponse,
    InvoiceResponse,
    InvoiceSummaryResponse,
    JobResponse,
    SupplierResponse,
)


def supplier_to_response(supplier: Supplier) -> SupplierResponse:
    return SupplierResponse(
        id=supplier.id,
        name=supplier.name,
        tax_id=supplier.tax_id,
        address=supplier.address,
        phone=supplier.phone,
        email=supplier.email,
    )


def invoice_to_response(invoice: Invoice, supplier: Supplier | None) -> InvoiceResponse:
    placeholder = SupplierResponse(
        id=invoice.supplier_id, name="(unknown)", tax_id="", address=None, phone=None, email=None
    )
    return InvoiceResponse(
        id=invoice.id,
        document_id=invoice.document_id,
        number=invoice.number,
        issue_date=invoice.issue_date,
        due_date=invoice.due_date,
        currency=invoice.currency,
        subtotal=float(invoice.subtotal.amount),
        tax=float(invoice.tax_amount.amount),
        total=float(invoice.total.amount),
        validation_report=invoice.validation_report,
        raw_extraction=invoice.raw_extraction,
        supplier=supplier_to_response(supplier) if supplier else placeholder,
        items=[
            InvoiceItemResponse(
                id=item.id,
                description=item.description,
                quantity=item.quantity,
                unit_price=float(item.unit_price),
                tax=float(item.tax_amount),
                total=float(item.total.amount),
            )
            for item in invoice.items
        ],
        created_at=invoice.created_at,
    )


def summary_to_response(summary: InvoiceSummary) -> InvoiceSummaryResponse:
    return InvoiceSummaryResponse(
        id=summary.id,
        document_id=summary.document_id,
        number=summary.number,
        issue_date=summary.issue_date,
        due_date=summary.due_date,
        currency=summary.currency,
        subtotal=float(summary.subtotal),
        tax=float(summary.tax_amount),
        total=float(summary.total),
        supplier_name=summary.supplier_name,
        supplier_tax_id=summary.supplier_tax_id,
    )


def job_to_response(job: ProcessingJob) -> JobResponse:
    return JobResponse(
        id=job.id,
        document_id=job.document_id,
        invoice_id=job.invoice_id,
        status=job.status.value,
        attempts=job.attempts,
        error_message=job.error_message,
        celery_task_id=job.celery_task_id,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def document_brief(document: Document) -> dict:
    return {"id": str(document.id), "filename": document.filename}
