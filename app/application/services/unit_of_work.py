"""Unit of Work port: atomic boundary over repositories."""

from abc import ABC, abstractmethod
from types import TracebackType

from app.domain.repositories.document_repository import DocumentRepository
from app.domain.repositories.invoice_repository import InvoiceRepository
from app.domain.repositories.job_repository import JobRepository
from app.domain.repositories.supplier_repository import SupplierRepository


class UnitOfWork(ABC):
    """
    Transactional scope aggregating all repositories.

    Use cases open it with `with` semantics; adapters decide the concrete
    technology (SQLAlchemy session today, something else tomorrow).
    """

    documents: DocumentRepository
    suppliers: SupplierRepository
    invoices: InvoiceRepository
    jobs: JobRepository

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @abstractmethod
    def __enter__(self) -> "UnitOfWork": ...

    @abstractmethod
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
