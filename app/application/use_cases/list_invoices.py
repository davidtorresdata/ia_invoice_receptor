"""Use case: filtered + paginated invoice listing."""

from collections.abc import Callable

from app.application.services.unit_of_work import UnitOfWork
from app.domain.repositories.invoice_repository import InvoiceListPage, InvoiceQuery


class ListInvoicesUseCase:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self, criteria: InvoiceQuery) -> InvoiceListPage:
        with self._uow_factory() as uow:
            return uow.invoices.query(criteria)
