"""SQLAlchemy-backed Unit of Work: one transaction per business operation."""

from types import TracebackType

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.application.services.unit_of_work import UnitOfWork
from app.domain.exceptions import PersistenceError
from app.infrastructure.repositories.sqlalchemy_document_repository import (
    SqlAlchemyDocumentRepository,
)
from app.infrastructure.repositories.sqlalchemy_invoice_repository import (
    SqlAlchemyInvoiceRepository,
)
from app.infrastructure.repositories.sqlalchemy_job_repository import SqlAlchemyJobRepository
from app.infrastructure.repositories.sqlalchemy_supplier_repository import (
    SqlAlchemySupplierRepository,
)


class SqlAlchemyUnitOfWork(UnitOfWork):
    """
    Context manager owning a Session and the four repositories.

    Usage:
        with uow as unit:
            unit.documents.add(doc)
            unit.jobs.add(job)
            unit.commit()          # explicit; rollback happens on exceptions
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

        self.documents: SqlAlchemyDocumentRepository | None = None
        self.suppliers: SqlAlchemySupplierRepository | None = None
        self.invoices: SqlAlchemyInvoiceRepository | None = None
        self.jobs: SqlAlchemyJobRepository | None = None

    # ------------------------------------------------------------------ ctx mgmt
    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._session_factory()
        self.documents = SqlAlchemyDocumentRepository(self._session)
        self.suppliers = SqlAlchemySupplierRepository(self._session)
        self.invoices = SqlAlchemyInvoiceRepository(self._session)
        self.jobs = SqlAlchemyJobRepository(self._session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                self.rollback()
        finally:
            if self._session is not None:
                self._session.close()
                self._session = None

    # ----------------------------------------------------------------- operations
    def commit(self) -> None:
        assert self._session is not None, "commit() outside of context manager"
        try:
            self._session.commit()
        except IntegrityError as exc:
            self.rollback()
            orig = getattr(exc, "orig", None)
            detail = str(getattr(orig, "sqlstate", "") or "")
            diag = getattr(orig, "diag", None)  # psycopg3 diagnostics
            detail += (
                f" {getattr(diag, 'message_primary', '') or ''}"
                f" (constraint: {getattr(diag, 'constraint_name', '?')})"
            ).rstrip() or repr(exc.orig)
            raise PersistenceError(
                f"Integrity constraint violated: {detail}",
                retryable=False,  # duplicates never heal by retrying
            ) from exc
        except SQLAlchemyError as exc:
            self.rollback()
            raise PersistenceError(f"Database failure: {exc}") from exc  # transient

    def rollback(self) -> None:
        if self._session is not None:
            self._session.rollback()
