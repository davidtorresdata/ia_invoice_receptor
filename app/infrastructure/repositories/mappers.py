"""Translators between ORM models and domain entities (anti-corruption layer).

All mapping rules live here so repositories stay thin and the domain never
leaks persistence concerns into its entities.
"""

from datetime import date, datetime
from decimal import Decimal

from app.domain.entities.document import Document
from app.domain.entities.invoice import Invoice, InvoiceItem, Supplier
from app.domain.entities.job import ProcessingJob
from app.domain.value_objects.enums import (
    DocumentStatus,
    DocumentType,
    JobStatus,
)
from app.domain.value_objects.money import Money
from app.infrastructure.database.models import (
    DocumentModel,
    InvoiceItemModel,
    InvoiceModel,
    ProcessingJobModel,
    SupplierModel,
)


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------
def document_to_domain(model: DocumentModel) -> Document:
    return Document(
        id=model.id,
        filename=model.filename,
        content_type=model.content_type,
        size_bytes=int(model.size_bytes),
        storage_path=model.storage_path,
        document_type=DocumentType(model.document_type),
        status=DocumentStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def apply_document(entity: Document, model: DocumentModel) -> None:
    model.filename = entity.filename
    model.content_type = entity.content_type
    model.size_bytes = entity.size_bytes
    model.storage_path = entity.storage_path
    model.document_type = DocumentType(entity.document_type)
    model.status = DocumentStatus(entity.status)


# ---------------------------------------------------------------------------
# Supplier
# ---------------------------------------------------------------------------
def supplier_to_domain(model: SupplierModel) -> Supplier:
    return Supplier(
        id=model.id,
        name=model.name,
        tax_id=model.tax_id,
        address=model.address,
        phone=model.phone,
        email=model.email,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def build_supplier_model(supplier: Supplier) -> SupplierModel:
    return SupplierModel(
        id=supplier.id,
        name=supplier.name,
        tax_id=supplier.tax_id,
        address=supplier.address,
        phone=supplier.phone,
        email=supplier.email,
    )


def apply_supplier(entity: Supplier, model: SupplierModel) -> None:
    model.name = entity.name
    model.address = entity.address
    model.phone = entity.phone
    model.email = entity.email


# ---------------------------------------------------------------------------
# Invoice + items
# ---------------------------------------------------------------------------
def invoice_to_domain(model: InvoiceModel) -> Invoice:
    # Items are built first: the aggregate's __post_init__ enforces
    # "at least one line item", so the entity cannot be created empty.
    items = [
        InvoiceItem(
            id=item_model.id,
            description=item_model.description,
            quantity=Decimal(item_model.quantity),
            unit_price=Decimal(item_model.unit_price),
            tax_amount=Decimal(item_model.tax_amount),
            total=Money(Decimal(item_model.total)),
        )
        for item_model in model.items
    ]
    return Invoice(
        id=model.id,
        document_id=model.document_id,
        supplier_id=model.supplier_id,
        number=model.number,
        issue_date=_as_date(model.issue_date),
        due_date=_as_optional_date(model.due_date),
        currency=model.currency,
        subtotal=Money(Decimal(model.subtotal)),
        tax_amount=Money(Decimal(model.tax_amount)),
        total=Money(Decimal(model.total)),
        validation_report=dict(model.validation_report) if model.validation_report else None,
        raw_extraction=dict(model.raw_extraction) if model.raw_extraction else None,
        created_at=model.created_at,
        updated_at=model.updated_at,
        items=items,
    )


def build_invoice_models(invoice: Invoice) -> tuple[InvoiceModel, list[InvoiceItemModel]]:
    """Create detached ORM objects for a full aggregate (caller adds them)."""
    model = InvoiceModel(
        id=invoice.id,
        document_id=invoice.document_id,
        supplier_id=invoice.supplier_id,
        number=invoice.number,
        issue_date=invoice.issue_date,
        due_date=invoice.due_date,
        currency=invoice.currency,
        subtotal=invoice.subtotal.amount,
        tax_amount=invoice.tax_amount.amount,
        total=invoice.total.amount,
        validation_report=invoice.validation_report,
        raw_extraction=invoice.raw_extraction,
    )
    item_models = [
        InvoiceItemModel(
            id=item.id,
            invoice_id=invoice.id,
            position=position,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
            tax_amount=item.tax_amount,
            total=item.total.amount,
        )
        for position, item in enumerate(invoice.items)
    ]
    return model, item_models


def summary_from_models(invoice_model: InvoiceModel) -> dict:
    """Flat projection used to assemble `InvoiceSummary` read models."""
    supplier = invoice_model.supplier
    return {
        "id": invoice_model.id,
        "document_id": invoice_model.document_id,
        "number": invoice_model.number,
        "issue_date": _as_date(invoice_model.issue_date),
        "due_date": _as_optional_date(invoice_model.due_date),
        "currency": invoice_model.currency,
        "subtotal": Decimal(invoice_model.subtotal),
        "tax_amount": Decimal(invoice_model.tax_amount),
        "total": Decimal(invoice_model.total),
        "supplier_name": supplier.name if supplier else "",
        "supplier_tax_id": supplier.tax_id if supplier else "",
    }


# ---------------------------------------------------------------------------
# Processing job
# ---------------------------------------------------------------------------
def job_to_domain(model: ProcessingJobModel) -> ProcessingJob:
    return ProcessingJob(
        id=model.id,
        document_id=model.document_id,
        status=JobStatus(model.status),
        attempts=int(model.attempts),
        invoice_id=model.invoice_id,
        celery_task_id=model.celery_task_id,
        error_message=model.error_message,
        started_at=model.started_at,
        finished_at=model.finished_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def apply_job(entity: ProcessingJob, model: ProcessingJobModel) -> None:
    model.status = JobStatus(entity.status)
    model.attempts = entity.attempts
    model.invoice_id = entity.invoice_id
    model.celery_task_id = entity.celery_task_id
    model.error_message = entity.error_message
    model.started_at = entity.started_at
    model.finished_at = entity.finished_at


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _as_optional_date(value: date | datetime | None) -> date | None:
    return _as_date(value) if value is not None else None
