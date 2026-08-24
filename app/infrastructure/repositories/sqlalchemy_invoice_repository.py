"""SQLAlchemy implementation of the InvoiceRepository port."""

import uuid
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.domain.entities.invoice import Invoice
from app.domain.repositories.invoice_repository import (
    InvoiceListPage,
    InvoiceQuery,
    InvoiceRepository,
    InvoiceStats,
    InvoiceSummary,
)
from app.infrastructure.database.models import InvoiceModel, SupplierModel
from app.infrastructure.repositories.mappers import (
    build_invoice_models,
    invoice_to_domain,
    summary_from_models,
)


class SqlAlchemyInvoiceRepository(InvoiceRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    # ----------------------------------------------------------------- writes
    def add(self, invoice: Invoice) -> None:
        model, item_models = build_invoice_models(invoice)
        self._session.add(model)
        self._session.add_all(item_models)

    # ------------------------------------------------------------------ reads
    def get(self, invoice_id: uuid.UUID) -> Invoice | None:
        model = self._session.get(InvoiceModel, invoice_id)
        return invoice_to_domain(model) if model else None

    def get_by_document(self, document_id: uuid.UUID) -> Invoice | None:
        model = self._find_model_by_document(document_id)
        return invoice_to_domain(model) if model else None

    def query(self, criteria: InvoiceQuery) -> InvoiceListPage:
        conditions = self._filters(criteria)

        base = select(InvoiceModel).join(
            InvoiceModel.supplier, isouter=True
        )
        filtered = base.where(*conditions)

        total = int(
            self._session.scalar(
                select(func.count()).select_from(filtered.order_by(None).subquery())
            )
            or 0
        )

        rows = self._session.scalars(
            filtered.order_by(InvoiceModel.issue_date.desc(), InvoiceModel.created_at.desc())
            .offset(criteria.offset)
            .limit(criteria.limit)
        ).all()

        items = [InvoiceSummary(**summary_from_models(row)) for row in rows]
        return InvoiceListPage(items=items, total_count=total)

    def stats(self) -> InvoiceStats:
        row = self._session.execute(
            select(
                func.count(InvoiceModel.id),
                func.coalesce(func.sum(InvoiceModel.total), Decimal("0")),
            )
        ).one()
        return InvoiceStats(
            total_invoices=int(row[0]),
            total_invoiced=Decimal(row[1]),
        )

    # ---------------------------------------------------------------- helpers
    def _find_model_by_document(self, document_id: uuid.UUID) -> InvoiceModel | None:
        stmt = (
            select(InvoiceModel)
            .where(InvoiceModel.document_id == document_id)
            .order_by(InvoiceModel.created_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    @staticmethod
    def _filters(criteria: InvoiceQuery) -> list:
        conditions = []
        if criteria.search:
            pattern = f"%{criteria.search.strip()}%"
            conditions.append(
                or_(
                    InvoiceModel.number.ilike(pattern),
                    SupplierModel.name.ilike(pattern),
                    SupplierModel.tax_id.ilike(pattern),
                )
            )
        if criteria.date_from:
            conditions.append(InvoiceModel.issue_date >= criteria.date_from)
        if criteria.date_to:
            conditions.append(InvoiceModel.issue_date <= criteria.date_to)
        return conditions
