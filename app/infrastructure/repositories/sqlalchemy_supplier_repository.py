"""SQLAlchemy implementation of the SupplierRepository port."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.entities.invoice import Supplier
from app.domain.repositories.supplier_repository import SupplierRepository
from app.infrastructure.database.models import SupplierModel
from app.infrastructure.repositories.mappers import build_supplier_model, supplier_to_domain


class SqlAlchemySupplierRepository(SupplierRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, supplier: Supplier) -> Supplier:
        self._session.add(build_supplier_model(supplier))
        return supplier

    def get(self, supplier_id: uuid.UUID) -> Supplier | None:
        model = self._session.get(SupplierModel, supplier_id)
        return supplier_to_domain(model) if model else None

    def find_by_tax_id(self, tax_id: str) -> Supplier | None:
        stmt = select(SupplierModel).where(SupplierModel.tax_id == tax_id.strip())
        model = self._session.scalars(stmt).first()
        return supplier_to_domain(model) if model else None
