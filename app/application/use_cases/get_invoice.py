"""Use case: fetch one processed invoice together with its supplier."""

from collections.abc import Callable
from uuid import UUID

from app.application.services.unit_of_work import UnitOfWork
from app.domain.entities.invoice import Invoice, Supplier
from app.domain.exceptions import InvoiceNotFoundError


class GetInvoiceUseCase:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self, invoice_id: UUID) -> tuple[Invoice, Supplier | None]:
        with self._uow_factory() as uow:
            invoice = uow.invoices.get(invoice_id)
            if invoice is None:
                raise InvoiceNotFoundError(f"Invoice {invoice_id} not found")
            supplier = uow.suppliers.get(invoice.supplier_id)
        return invoice, supplier
