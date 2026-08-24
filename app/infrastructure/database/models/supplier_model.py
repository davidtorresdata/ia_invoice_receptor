"""ORM model for suppliers (deduplicated by tax_id)."""

import uuid

from sqlalchemy import String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, TimestampMixin


class SupplierModel(Base, TimestampMixin):
    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tax_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    address: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(255))
