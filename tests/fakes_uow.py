"""In-memory Unit of Work + repositories (test doubles)."""

from decimal import Decimal
from types import TracebackType
from uuid import UUID

from app.application.services.unit_of_work import UnitOfWork
from app.domain.entities.document import Document
from app.domain.entities.invoice import Invoice, Supplier
from app.domain.entities.job import ProcessingJob
from app.domain.repositories.invoice_repository import (
    InvoiceListPage,
    InvoiceQuery,
    InvoiceRepository,
    InvoiceStats,
    InvoiceSummary,
)


class InMemoryDocumentRepository:
    def __init__(self, store: dict[UUID, Document]) -> None:
        self._store = store

    def add(self, document: Document) -> None:
        self._store[document.id] = document

    def get(self, document_id: UUID) -> Document | None:
        return self._store.get(document_id)

    def update(self, document: Document) -> None:
        if document.id not in self._store:
            raise KeyError(f"unknown document {document.id}")
        self._store[document.id] = document


class InMemorySupplierRepository:
    def __init__(self, store: dict[UUID, Supplier]) -> None:
        self._store = store

    def add(self, supplier: Supplier) -> Supplier:
        self._store[supplier.id] = supplier
        return supplier

    def get(self, supplier_id: UUID) -> Supplier | None:
        return self._store.get(supplier_id)

    def find_by_tax_id(self, tax_id: str) -> Supplier | None:
        return next((s for s in self._store.values() if s.tax_id == tax_id), None)


class InMemoryInvoiceRepository(InvoiceRepository):
    def __init__(self, store: dict[UUID, Invoice],
                 suppliers: dict[UUID, Supplier]) -> None:
        self._store = store
        self._suppliers = suppliers

    def add(self, invoice: Invoice) -> None:
        self._store[invoice.id] = invoice

    def get(self, invoice_id: UUID) -> Invoice | None:
        return self._store.get(invoice_id)

    def get_by_document(self, document_id: UUID) -> Invoice | None:
        return next(
            (i for i in self._store.values() if i.document_id == document_id), None
        )

    def query(self, criteria: InvoiceQuery) -> InvoiceListPage:
        rows = [self._summary(i) for i in self._store.values()]

        search = (criteria.search or "").strip().lower()
        if search:
            rows = [
                r for r in rows
                if search in r.number.lower()
                or search in r.supplier_name.lower()
                or search in r.supplier_tax_id.lower()
            ]
        if criteria.date_from:
            rows = [r for r in rows if r.issue_date >= criteria.date_from]
        if criteria.date_to:
            rows = [r for r in rows if r.issue_date <= criteria.date_to]

        rows.sort(key=lambda r: (r.issue_date, r.total), reverse=True)
        window = rows[criteria.offset:criteria.offset + criteria.limit]
        return InvoiceListPage(items=window, total_count=len(rows))

    def stats(self) -> InvoiceStats:
        invoices = list(self._store.values())
        total_amount = sum(
            (i.total.amount for i in invoices), start=Decimal("0")
        )
        return InvoiceStats(
            total_invoices=len(invoices),
            total_invoiced=total_amount,
        )

    def _summary(self, invoice: Invoice) -> InvoiceSummary:
        supplier = self._suppliers.get(invoice.supplier_id)
        return InvoiceSummary(
            id=invoice.id,
            document_id=invoice.document_id,
            number=invoice.number,
            issue_date=invoice.issue_date,
            due_date=invoice.due_date,
            currency=invoice.currency,
            subtotal=invoice.subtotal.amount,
            tax_amount=invoice.tax_amount.amount,
            total=invoice.total.amount,
            supplier_name=supplier.name if supplier else "",
            supplier_tax_id=supplier.tax_id if supplier else "",
        )


class InMemoryJobRepository:
    def __init__(self, store: dict[UUID, ProcessingJob]) -> None:
        self._store = store

    def add(self, job: ProcessingJob) -> None:
        self._store[job.id] = job

    def get(self, job_id: UUID) -> ProcessingJob | None:
        return self._store.get(job_id)

    def update(self, job: ProcessingJob) -> None:
        if job.id not in self._store:
            raise KeyError(f"unknown job {job.id}")
        self._store[job.id] = job

    def count_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in self._store.values():
            key = str(job.status)
            counts[key] = counts.get(key, 0) + 1
        return counts


class FakeStore:
    """Shared mutable state simulating one database across transactions."""

    def __init__(self) -> None:
        self.documents: dict[UUID, Document] = {}
        self.suppliers: dict[UUID, Supplier] = {}
        self.invoices: dict[UUID, Invoice] = {}
        self.jobs: dict[UUID, ProcessingJob] = {}


class FakeUnitOfWork(UnitOfWork):
    """
    Multiple instances wrapping the SAME `FakeStore` behave like separate
    sessions/transactions against one database — exactly what use cases expect.
    """

    def __init__(self, store: FakeStore | None = None) -> None:
        self._store = store or FakeStore()

        self.documents = InMemoryDocumentRepository(self._store.documents)
        self.suppliers = InMemorySupplierRepository(self._store.suppliers)
        self.invoices = InMemoryInvoiceRepository(
            self._store.invoices, self._store.suppliers
        )
        self.jobs = InMemoryJobRepository(self._store.jobs)

        self.commit_count = 0
        self.rollback_count = 0

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def __enter__(self) -> "FakeUnitOfWork":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollback()
