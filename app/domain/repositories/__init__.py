"""Repository ports (driven interfaces implemented in infrastructure)."""

from app.domain.repositories.document_repository import DocumentRepository
from app.domain.repositories.invoice_repository import (
    InvoiceListPage,
    InvoiceQuery,
    InvoiceRepository,
    InvoiceStats,
)
from app.domain.repositories.job_repository import JobRepository
from app.domain.repositories.supplier_repository import SupplierRepository

__all__ = [
    "DocumentRepository",
    "InvoiceListPage",
    "InvoiceQuery",
    "InvoiceRepository",
    "InvoiceStats",
    "JobRepository",
    "SupplierRepository",
]
