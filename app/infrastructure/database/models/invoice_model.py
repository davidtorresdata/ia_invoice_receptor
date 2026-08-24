"""ORM models for invoices and their line items."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.base import Base, TimestampMixin
from app.infrastructure.database.models.supplier_model import SupplierModel


class InvoiceModel(Base, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("supplier_id", "number", name="uq_invoices_supplier_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)

    # 1:1 with the source document (idempotency anchor for the pipeline).
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    number: Mapped[str] = mapped_column(String(100), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    validation_report: Mapped[dict | None] = mapped_column(JSONB)
    raw_extraction: Mapped[dict | None] = mapped_column(JSONB)  # audited LLM payload

    items: Mapped[list["InvoiceItemModel"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceItemModel.position",
        lazy="selectin",
    )

    # Real relationship (NOT viewonly): gives the Session's unit of work an
    # explicit dependency edge so suppliers is always INSERTed before
    # invoices. FK-only ordering across unrelated mappers is NOT guaranteed.
    supplier: Mapped[SupplierModel] = relationship()


class InvoiceItemModel(Base):
    __tablename__ = "invoice_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    invoice: Mapped[InvoiceModel] = relationship(back_populates="items")
