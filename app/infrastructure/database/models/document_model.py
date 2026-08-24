"""ORM model for uploaded documents."""

import uuid

from sqlalchemy import BigInteger, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.value_objects.enums import DocumentStatus, DocumentType
from app.infrastructure.database.base import Base, TimestampMixin, str_enum


class DocumentModel(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    document_type: Mapped[DocumentType] = mapped_column(str_enum(DocumentType), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(str_enum(DocumentStatus), nullable=False)


Index("ix_documents_status_created_at", DocumentModel.status, DocumentModel.created_at.desc())
