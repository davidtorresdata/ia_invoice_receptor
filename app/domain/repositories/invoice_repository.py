"""Port for invoice aggregate persistence + read models."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.domain.entities.invoice import Invoice


@dataclass(frozen=True, slots=True)
class InvoiceQuery:
    """Listing/filter parameters (transport-agnostic)."""

    search: str | None = None          # matches invoice number or supplier name/tax_id
    date_from: date | None = None
    date_to: date | None = None
    limit: int = 20
    offset: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 100:
            raise ValueError("limit must be within [1, 100]")
        if self.offset < 0:
            raise ValueError("offset must be >= 0")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not exceed date_to")


@dataclass(frozen=True, slots=True)
class InvoiceSummary:
    """Flat read model for listings (avoids loading full aggregates)."""

    id: UUID
    document_id: UUID
    number: str
    issue_date: date
    due_date: date | None
    currency: str
    subtotal: Decimal
    tax_amount: Decimal
    total: Decimal
    supplier_name: str
    supplier_tax_id: str


@dataclass(frozen=True, slots=True)
class InvoiceListPage:
    items: list[InvoiceSummary]
    total_count: int


@dataclass(frozen=True, slots=True)
class InvoiceStats:
    total_invoices: int
    total_invoiced: Decimal


class InvoiceRepository(ABC):
    @abstractmethod
    def add(self, invoice: Invoice) -> None:
        """Persist an invoice together with its line items."""

    @abstractmethod
    def get(self, invoice_id: UUID) -> Invoice | None:
        """Fetch an invoice with items eagerly loaded."""

    @abstractmethod
    def get_by_document(self, document_id: UUID) -> Invoice | None:
        """Idempotency/resume guard: one invoice per processed document."""

    @abstractmethod
    def query(self, criteria: InvoiceQuery) -> InvoiceListPage:
        """Filtered + paginated listing (newest first)."""

    @abstractmethod
    def stats(self) -> InvoiceStats:
        """Aggregates for dashboards."""
